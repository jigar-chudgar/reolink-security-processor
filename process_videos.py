import os
import io
import json
import shutil
import subprocess
import urllib.request
import base64

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

from PIL import Image, ImageDraw


SCOPES = [
    "https://www.googleapis.com/auth/drive"
]

ROOT_FOLDER_ID = os.environ["REOLINK_DRIVE_ROOT_ID"]

APPS_SCRIPT_URL = os.environ["REOLINK_APPS_SCRIPT_URL"]

WEBAPP_TOKEN = os.environ["REOLINK_WEBAPP_TOKEN"]

# Maximum number of times a video is retried after a failure
# before it is given up on and moved to ProcessedVideos anyway.
MAX_RETRIES = 3

# Number of evenly-spaced frames to capture per video for the
# contact sheet, regardless of the video's exact duration.
CONTACT_SHEET_FRAME_COUNT = 6


creds = service_account.Credentials.from_service_account_file(
    "service_account.json",
    scopes=SCOPES
)

drive = build(
    "drive",
    "v3",
    credentials=creds
)


# Stores screenshots for this GitHub run
processed_screenshots = []

# Stores videos that were given up on after MAX_RETRIES failures,
# so a lightweight note can be included in the summary email.
failed_videos = []


def parse_camera_and_time(video_name, date_name):

    # Filenames are timestamp-first:
    # 2026-08-02_22-16-41_Front_Door.mp4
    # 2026-08-02_22-18-03_Garage.mp4

    name_without_extension = os.path.splitext(video_name)[0]

    parts = name_without_extension.split("_")

    if len(parts) >= 3:

        date_part = parts[0]

        time_part = parts[1]

        # Everything after date/time is the camera name,
        # joined back together in case it has underscores
        # (e.g. "Front_Door" -> "Front Door").
        camera = "_".join(parts[2:]).replace("_", " ")

        time_display = (
            date_part +
            " " +
            time_part.replace("-", ":")
        )

    else:

        camera = "Camera"

        time_display = date_name

    return camera, time_display


def list_folders(parent_id):

    query = (
        f"'{parent_id}' in parents "
        "and mimeType='application/vnd.google-apps.folder'"
    )

    result = drive.files().list(
        q=query,
        fields="files(id,name)"
    ).execute()

    return result.get("files", [])


def list_videos(folder_id):

    query = (
        f"'{folder_id}' in parents "
        "and mimeType='video/mp4'"
    )

    result = drive.files().list(
        q=query,
        fields="files(id,name)"
    ).execute()

    return result.get("files", [])


def download_file(file_id, filename):

    # Get expected size from Drive metadata so we can verify
    # the download actually completed fully.
    file_meta = drive.files().get(
        fileId=file_id,
        fields="size"
    ).execute()

    expected_size = int(file_meta.get("size", 0))

    request = drive.files().get_media(
        fileId=file_id
    )

    with io.FileIO(filename, "wb") as fh:

        downloader = MediaIoBaseDownload(
            fh,
            request
        )

        done = False

        while not done:

            status, done = downloader.next_chunk()

            if status:
                print(
                    f"Downloaded {int(status.progress() * 100)}%"
                )

    actual_size = os.path.getsize(filename)

    if expected_size and actual_size != expected_size:

        raise RuntimeError(
            f"Download size mismatch for {filename}: "
            f"expected {expected_size} bytes, got {actual_size} bytes"
        )


def validate_mp4(video_file):

    if not os.path.exists(video_file) or os.path.getsize(video_file) == 0:

        raise RuntimeError(
            f"{video_file} is missing or empty after download"
        )

    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            video_file
        ],
        capture_output=True,
        text=True
    )

    if result.returncode != 0 or not result.stdout.strip():

        raise RuntimeError(
            f"ffprobe validation failed for {video_file}: "
            f"{result.stderr.strip()}"
        )

    try:

        duration = float(result.stdout.strip())

    except ValueError:

        raise RuntimeError(
            f"ffprobe returned a non-numeric duration for "
            f"{video_file}: {result.stdout.strip()}"
        )

    if duration <= 0:

        raise RuntimeError(
            f"ffprobe reported an invalid duration ({duration}) "
            f"for {video_file}"
        )

    return duration


def extract_contact_sheet(video_file, jpg_file, duration):

    work_dir = (
        "frames_" +
        os.path.splitext(video_file)[0]
    )

    os.makedirs(work_dir, exist_ok=True)

    # Compute an interval that yields CONTACT_SHEET_FRAME_COUNT
    # evenly-spaced frames across the video's actual duration,
    # instead of assuming every clip is exactly 30 seconds.
    interval = max(
        duration / CONTACT_SHEET_FRAME_COUNT,
        0.1
    )

    fps = 1 / interval

    subprocess.run([
        "ffmpeg",
        "-y",
        "-i",
        video_file,
        "-vf",
        f"fps={fps}",
        f"{work_dir}/frame_%02d.jpg"
    ], check=True)

    images = []

    for filename in sorted(os.listdir(work_dir)):

        if filename.endswith(".jpg"):

            path = os.path.join(
                work_dir,
                filename
            )

            img = Image.open(path)

            img.thumbnail((320, 180))

            images.append(img.copy())

            img.close()

    if not images:

        shutil.rmtree(work_dir, ignore_errors=True)

        raise RuntimeError(
            "FFmpeg did not create any screenshots"
        )

    columns = 3
    cell_width = 320
    cell_height = 220

    rows = (
        len(images) +
        columns - 1
    ) // columns

    sheet = Image.new(
        "RGB",
        (
            columns * cell_width,
            rows * cell_height
        ),
        "white"
    )

    draw = ImageDraw.Draw(sheet)

    for i, img in enumerate(images):

        x = (
            i % columns
        ) * cell_width

        y = (
            i // columns
        ) * cell_height

        sheet.paste(
            img,
            (x, y)
        )

        draw.text(
            (x + 5, y + 185),
            f"{i * interval:.1f} sec",
            fill="black"
        )

    sheet.save(
        jpg_file,
        "JPEG"
    )

    shutil.rmtree(
        work_dir
    )


def send_to_apps_script(
    date,
    filename,
    jpg_file
):

    print(
        "Sending screenshot to Apps Script..."
    )

    with open(jpg_file, "rb") as f:

        image_base64 = base64.b64encode(
            f.read()
        ).decode("utf-8")


    payload = {
        "token": WEBAPP_TOKEN,
        "action": "upload",
        "date": date,
        "filename": filename,
        "image": image_base64
    }


    data = json.dumps(
        payload
    ).encode("utf-8")


    request = urllib.request.Request(
        APPS_SCRIPT_URL,
        data=data,
        headers={
            "Content-Type":
                "application/json"
        },
        method="POST"
    )


    with urllib.request.urlopen(
        request,
        timeout=120
    ) as response:

        response_text = (
            response.read()
            .decode("utf-8")
        )


    print(
        "Apps Script response:",
        response_text
    )


    result = json.loads(
        response_text
    )


    if not result.get("success"):

        raise RuntimeError(
            "Apps Script failed: "
            + str(result)
        )


    print(
        "Screenshot successfully saved to Drive:",
        result.get("filename")
    )


    return image_base64


def send_summary_email():

    if not processed_screenshots and not failed_videos:

        print(
            "No videos processed. "
            "No email will be sent."
        )

        return


    print(
        f"Sending summary email with "
        f"{len(processed_screenshots)} event(s) and "
        f"{len(failed_videos)} failed video(s)..."
    )


    screenshots = []


    for item in processed_screenshots:

        screenshots.append({

            "filename":
                item["filename"],

            "camera":
                item["camera"],

            "time":
                item["time"],

            "image":
                item["image"]

        })


    payload = {

        "token":
            WEBAPP_TOKEN,

        "action":
            "email",

        "screenshots":
            screenshots,

        "failed":
            failed_videos

    }


    data = json.dumps(
        payload
    ).encode("utf-8")


    request = urllib.request.Request(
        APPS_SCRIPT_URL,
        data=data,
        headers={
            "Content-Type":
                "application/json"
        },
        method="POST"
    )


    with urllib.request.urlopen(
        request,
        timeout=120
    ) as response:

        response_text = (
            response.read()
            .decode("utf-8")
        )


    print(
        "Email response:",
        response_text
    )


    result = json.loads(
        response_text
    )


    if not result.get("success"):

        raise RuntimeError(
            "Email failed: "
            + str(result)
        )


    print(
        "Summary email sent successfully."
    )


def find_or_create_folder(
    name,
    parent_id
):

    query = (
        f"'{parent_id}' in parents "
        f"and name='{name}' "
        "and mimeType="
        "'application/vnd.google-apps.folder'"
    )

    result = drive.files().list(
        q=query,
        fields="files(id,name)"
    ).execute()

    files = result.get(
        "files",
        []
    )

    if files:

        return files[0]["id"]


    metadata = {

        "name":
            name,

        "mimeType":
            "application/vnd.google-apps.folder",

        "parents":
            [parent_id]

    }


    folder = drive.files().create(
        body=metadata,
        fields="id"
    ).execute()


    return folder["id"]


def get_retry_count(file_id):

    file = drive.files().get(
        fileId=file_id,
        fields="appProperties"
    ).execute()

    props = file.get("appProperties") or {}

    return int(props.get("retry_count", 0))


def set_retry_count(file_id, count):

    drive.files().update(
        fileId=file_id,
        body={
            "appProperties": {
                "retry_count": str(count)
            }
        }
    ).execute()


def move_file(
    file_id,
    new_folder_id
):

    file = drive.files().get(
        fileId=file_id,
        fields="parents"
    ).execute()


    previous_parents = ",".join(
        file.get(
            "parents",
            []
        )
    )


    drive.files().update(
        fileId=file_id,
        addParents=new_folder_id,
        removeParents=previous_parents,
        fields="id,parents"
    ).execute()


print(
    "Searching videos..."
)


date_folders = list_folders(
    ROOT_FOLDER_ID
)

date_folders.sort(
    key=lambda folder: folder["name"]
)

for date_folder in date_folders:

    date_id = date_folder["id"]

    date_name = date_folder["name"]

    subfolders = list_folders(
        date_id
    )


    unprocessed_id = None


    for folder in subfolders:

        if folder["name"] == "UnprocessedVideos":

            unprocessed_id = folder["id"]


    if not unprocessed_id:

        continue


    videos = list_videos(
        unprocessed_id
    )

    # Filenames are timestamp-first, e.g.
    # 2026-08-02_22-16-41_Front_Door.mp4
    # so alphabetical sort == chronological order.
    videos.sort(
         key=lambda video: video["name"].lower()
    )


    for video in videos:

        print(
            "\nProcessing:",
            video["name"]
        )


        local_file = video["name"]

        jpg_file = local_file.replace(
            ".mp4",
            ".jpg"
        )


        try:

            # Download MP4 (raises if size doesn't match Drive metadata)
            download_file(
                video["id"],
                local_file
            )


            # Validate the file is a complete, readable video
            # before handing it to FFmpeg for frame extraction.
            duration = validate_mp4(
                local_file
            )


            # Create contact sheet
            extract_contact_sheet(
                local_file,
                jpg_file,
                duration
            )


            print(
                "Created contact sheet:",
                jpg_file
            )


            # Upload screenshot to Drive
            image_base64 = send_to_apps_script(
                date_name,
                jpg_file,
                jpg_file
            )


            # Extract camera name and time from filename.
            camera, time_display = parse_camera_and_time(
                video["name"],
                date_name
            )


            processed_screenshots.append({

                "filename":
                    jpg_file,

                "camera":
                    camera,

                "time":
                    time_display,

                "image":
                    image_base64

            })


            # Find/create ProcessedVideos
            processed_id = (
                find_or_create_folder(
                    "ProcessedVideos",
                    date_id
                )
            )


            # Move only after screenshot succeeded
            move_file(
                video["id"],
                processed_id
            )


            print(
                "Moved MP4 to ProcessedVideos"
            )


        except Exception as e:

            # Isolate failures per-video so one bad file doesn't
            # kill the whole run (and the summary email for
            # everything else that succeeded).

            print(
                f"FAILED processing {video['name']}: {e}"
            )

            try:

                retry_count = get_retry_count(video["id"]) + 1

                if retry_count >= MAX_RETRIES:

                    print(
                        f"Giving up on {video['name']} after "
                        f"{retry_count} failed attempts. "
                        "Moving to ProcessedVideos without a screenshot."
                    )

                    camera, time_display = parse_camera_and_time(
                        video["name"],
                        date_name
                    )

                    failed_videos.append({
                        "filename": video["name"],
                        "camera": camera,
                        "time": time_display,
                        "attempts": retry_count
                    })

                    processed_id = find_or_create_folder(
                        "ProcessedVideos",
                        date_id
                    )

                    move_file(
                        video["id"],
                        processed_id
                    )

                else:

                    set_retry_count(
                        video["id"],
                        retry_count
                    )

                    print(
                        f"Recorded retry {retry_count}/{MAX_RETRIES} "
                        f"for {video['name']}. Will retry next run."
                    )

            except Exception as retry_error:

                # Don't let a failure in the retry-tracking logic
                # itself take down the whole run.
                print(
                    f"Could not update retry tracking for "
                    f"{video['name']}: {retry_error}"
                )

            continue


        finally:

            if os.path.exists(
                local_file
            ):

                os.remove(
                    local_file
                )


            if os.path.exists(
                jpg_file
            ):

                os.remove(
                    jpg_file
                )


        print(
            "Completed:",
            video["name"]
        )


# ------------------------------------------------
# All videos finished (or failed individually).
# Send ONE summary email for whatever succeeded.
# ------------------------------------------------

send_summary_email()
