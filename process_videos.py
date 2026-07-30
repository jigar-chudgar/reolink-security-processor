import os
import io
import subprocess
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
from PIL import Image, ImageDraw, ImageFont


SCOPES = [
    "https://www.googleapis.com/auth/drive"
]

ROOT_FOLDER_ID = os.environ["REOLINK_DRIVE_ROOT_ID"]


creds = service_account.Credentials.from_service_account_file(
    "service_account.json",
    scopes=SCOPES
)

drive = build(
    "drive",
    "v3",
    credentials=creds
)


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
                    f"Downloaded {int(status.progress()*100)}%"
                )



def create_contact_sheet(video_file, output_file):

    os.makedirs("frames", exist_ok=True)

    subprocess.run([
        "ffmpeg",
        "-y",
        "-i",
        video_file,
        "-vf",
        "fps=1/6",
        "frames/frame_%02d.jpg"
    ],
    check=True)


    images = []

    for filename in sorted(os.listdir("frames")):

        if filename.endswith(".jpg"):

            img = Image.open(
                "frames/" + filename
            )

            img.thumbnail((320,180))

            images.append(img)


    sheet = Image.new(
        "RGB",
        (960, 400),
        "white"
    )


    draw = ImageDraw.Draw(sheet)


    for index, img in enumerate(images):

        x = (index % 3) * 320
        y = (index // 3) * 200

        sheet.paste(img,(x,y))

        draw.text(
            (x+5,y+185),
            f"{index*6} sec",
            fill="black"
        )


    sheet.save(output_file)



print("Searching videos...")


date_folders = list_folders(ROOT_FOLDER_ID)


for date_folder in date_folders:

    subfolders = list_folders(
        date_folder["id"]
    )

    for folder in subfolders:

        if folder["name"] == "UnprocessedVideos":

            videos = list_videos(
                folder["id"]
            )


            for video in videos:

                print(
                    "Processing:",
                    video["name"]
                )


                local_mp4 = video["name"]

                download_file(
                    video["id"],
                    local_mp4
                )


                jpg_name = (
                    video["name"]
                    .replace(".mp4",".jpg")
                )


                create_contact_sheet(
                    local_mp4,
                    jpg_name
                )


                print(
                    "Created:",
                    jpg_name
                )
