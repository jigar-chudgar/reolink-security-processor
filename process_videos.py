import os
import io
import shutil
import subprocess
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload, MediaFileUpload
from PIL import Image, ImageDraw


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



def find_or_create_folder(name, parent_id):

    query = (
        f"'{parent_id}' in parents "
        f"and name='{name}' "
        "and mimeType='application/vnd.google-apps.folder'"
    )

    result = drive.files().list(
        q=query,
        fields="files(id,name)"
    ).execute()


    files = result.get("files", [])

    if files:
        return files[0]["id"]


    metadata = {
        "name": name,
        "mimeType": "application/vnd.google-apps.folder",
        "parents": [parent_id]
    }


    folder = drive.files().create(
        body=metadata,
        fields="id"
    ).execute()

    return folder["id"]



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



def extract_contact_sheet(video_file, jpg_file):

    work_dir = "frames_" + video_file.replace(".mp4","")

    os.makedirs(work_dir, exist_ok=True)


    subprocess.run([
        "ffmpeg",
        "-y",
        "-i",
        video_file,
        "-vf",
        "fps=1/6",
        f"{work_dir}/frame_%02d.jpg"
    ],
    check=True)


    images=[]

    for f in sorted(os.listdir(work_dir)):

        if f.endswith(".jpg"):

            img = Image.open(
                os.path.join(work_dir,f)
            )

            img.thumbnail(
                (320,180)
            )

            images.append(img)


    width = 960
    height = ((len(images)+2)//3)*220


    sheet = Image.new(
        "RGB",
        (width,height),
        "white"
    )


    draw = ImageDraw.Draw(sheet)


    for i,img in enumerate(images):

        x=(i%3)*320
        y=(i//3)*220

        sheet.paste(img,(x,y))

        draw.text(
            (x+5,y+185),
            f"{i*6} sec",
            fill="black"
        )


    sheet.save(jpg_file)


    shutil.rmtree(work_dir)



def upload_file(filename, folder_id):

    metadata = {
        "name": filename,
        "parents": [folder_id]
    }


    media = MediaFileUpload(
        filename,
        mimetype="image/jpeg"
    )


    result = drive.files().create(
        body=metadata,
        media_body=media,
        fields="id"
    ).execute()


    return result["id"]



def move_file(file_id, new_folder_id):

    file = drive.files().get(
        fileId=file_id,
        fields="parents"
    ).execute()


    previous_parents = ",".join(
        file.get("parents")
    )


    drive.files().update(
        fileId=file_id,
        addParents=new_folder_id,
        removeParents=previous_parents,
        fields="id, parents"
    ).execute()



print("Searching videos...")


date_folders = list_folders(ROOT_FOLDER_ID)


for date_folder in date_folders:

    date_id = date_folder["id"]

    subfolders = list_folders(date_id)


    unprocessed_id = None


    for folder in subfolders:

        if folder["name"] == "UnprocessedVideos":
            unprocessed_id = folder["id"]


    if not unprocessed_id:
        continue


    videos = list_videos(unprocessed_id)


    for video in videos:

        print(
            "Processing:",
            video["name"]
        )


        local_file = video["name"]

        download_file(
            video["id"],
            local_file
        )


        jpg_file = local_file.replace(
            ".mp4",
            ".jpg"
        )


        extract_contact_sheet(
            local_file,
            jpg_file
        )


        screenshots_id = find_or_create_folder(
            "Screenshots",
            date_id
        )


        processed_id = find_or_create_folder(
            "ProcessedVideos",
            date_id
        )


        upload_file(
            jpg_file,
            screenshots_id
        )


        move_file(
            video["id"],
            processed_id
        )


        os.remove(local_file)
        os.remove(jpg_file)


        print(
            "Completed:",
            video["name"]
        )
