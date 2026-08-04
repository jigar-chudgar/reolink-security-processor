import os
import io
import json
import shutil
import subprocess
import urllib.request
import base64
import time

from datetime import datetime, timedelta

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

# Contact sheet layout / resolution. Fewer columns means each
# photo displays bigger in the email (the email caps display
# width, so column count controls perceived size more than
# raw resolution does).
CONTACT_SHEET_COLUMNS = 2
THUMBNAIL_MAX_SIZE = (480, 270)
CELL_WIDTH = 480
CELL_HEIGHT = 320
JPEG_QUALITY = 88

# Date folders older than this are deleted entirely (videos,
# screenshots, everything) on each run.
RETENTION_DAYS = 15


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

            img.thumbnail(THUMBNAIL_MAX_SIZE)

            images.append(img.copy())

            img.close()

    if not images:

        shutil.rmtree(work_dir, ignore_errors=True)

        raise RuntimeError(
            "FFmpeg did not create any screenshots"
        )

    columns = CONTACT_SHEET_COLUMNS
    cell_width = CELL_WIDTH
    cell_height = CELL_HEIGHT

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
            (x + 5, y + THUMBNAIL_MAX_SIZE[1] + 5),
            f"{i * interval:.1f} sec",
            fill="black"
        )

    sheet.save(
        jpg_file,
        "JPEG",
        quality=JPEG_QUALITY,
        optimize=True
    )

    shutil.rmtree(
        work_dir
    )


def call_apps_script(payload, timeout=120, max_attempts=3, retry_delay_seconds=5):

    # POSTs to Apps Script and parses the JSON response. Apps
    # Script web apps are known to intermittently return an
    # HTML error page (via a flaky internal redirect) instead
    # of the actual script output, even when the request itself
    # was fine. Retrying a couple of times handles that without
    # treating it as a hard failure on the first hiccup.

    data = json.dumps(payload).encode("utf-8")

    last_error = None

    for attempt in range(1, max_attempts + 1):

        try:

            request = urllib.request.Request(
                APPS_SCRIPT_URL,
                data=data,
                headers={
                    "Content-Type": "application/json"
                },
                method="POST"
            )

            with urllib.request.urlopen(
                request,
                timeout=timeout
            ) as response:

                response_text = (
                    response.read()
                    .decode("utf-8")
                )

            result = json.loads(response_text)

            return result

        except Exception as e:

            last_error = e

            print(
                f"Apps Script call failed on attempt "
                f"{attempt}/{max_attempts}: {e}"
            )

            if attempt < max_attempts:

                time.sleep(retry_delay_seconds)

    raise RuntimeError(
        f"Apps Script call failed after {max_attempts} "
        f"attempts: {last_error}"
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


    result = call_apps_script(payload)


    print(
        "Apps Script response:",
        result
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
                item["image"],

            "videoLink":
                item.get("videoLink")

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


    result = call_apps_script(payload)


    print(
        "Email response:",
        result
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


def get_video_link(file_id):

    # Returns Drive's built-in "view in browser" link for the
    # file. Relies on the file already being accessible to
    # whoever opens it (e.g. via the shared ReoLinkSecurityCamera
    # folder), same as browsing to it manually in Drive.

    file = drive.files().get(
        fileId=file_id,
        fields="webViewLink"
    ).execute()

    return file.get("webViewLink")


def delete_old_date_folders(date_folders):

    # Moves date folders (and everything inside them) to Trash
    # once they're older than RETENTION_DAYS, rather than
    # deleting permanently — gives a grace period to recover
    # something before Google's Trash auto-purges it (~30 days).
    # Folder names are expected in YYYY-MM-DD format; anything
    # that doesn't parse as a date is left alone rather than
    # risk trashing the wrong thing.

    cutoff_date = (
        datetime.utcnow().date() -
        timedelta(days=RETENTION_DAYS)
    )

    for date_folder in date_folders:

        date_name = date_folder["name"]

        try:

            folder_date = datetime.strptime(
                date_name,
                "%Y-%m-%d"
            ).date()

        except ValueError:

            print(
                f"Skipping cleanup for '{date_name}' "
                "(not a YYYY-MM-DD folder name)"
            )

            continue

        if folder_date < cutoff_date:

            print(
                f"Moving '{date_name}' to Trash "
                f"(older than {RETENTION_DAYS} days)"
            )

            try:

                # Trashing a folder via the Drive API also
                # trashes everything inside it.
                drive.files().update(
                    fileId=date_folder["id"],
                    body={"trashed": True}
                ).execute()

            except Exception as e:

                print(
                    f"Failed to trash '{date_name}': {e}"
                )


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


            # Grab a shareable link to the now-moved video so it
            # can be included in the summary email.
            video_link = get_video_link(
                video["id"]
            )


            processed_screenshots.append({

                "filename":
                    jpg_file,

                "camera":
                    camera,

                "time":
                    time_display,

                "image":
                    image_base64,

                "videoLink":
                    video_link

            })


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

email_failed = False

try:

    send_summary_email()

except Exception as e:

    # Videos were already downloaded, screenshotted, and moved
    # successfully above — don't let a flaky email delivery
    # erase that work or crash before cleanup runs.

    email_failed = True

    print(
        f"\nFAILED to send summary email: {e}"
    )


# ------------------------------------------------
# Clean up old data past the retention window.
# ------------------------------------------------

print(
    f"\nChecking for folders older than {RETENTION_DAYS} days..."
)

delete_old_date_folders(date_folders)


if email_failed:

    # Still surface this as a failed run in GitHub Actions so
    # it's noticed, but only after everything else completed.
    raise SystemExit(
        "Video processing succeeded but the summary email "
        "failed to send after retries. See log above."
    )
