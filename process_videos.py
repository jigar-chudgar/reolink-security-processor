import os
from google.oauth2 import service_account
from googleapiclient.discovery import build


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



def list_files(parent_id):

    query = (
        f"'{parent_id}' in parents "
        "and mimeType='video/mp4'"
    )

    result = drive.files().list(
        q=query,
        fields="files(id,name)"
    ).execute()

    return result.get("files", [])



print("Searching ReoLinkSecurityCamera")


dates = list_folders(ROOT_FOLDER_ID)


for date_folder in dates:

    print("\nDate folder:", date_folder["name"])

    subfolders = list_folders(date_folder["id"])

    for folder in subfolders:

        if folder["name"] == "UnprocessedVideos":

            print("Checking:", folder["name"])

            videos = list_files(folder["id"])

            if not videos:
                print("  No videos found")

            for video in videos:
                print(
                    "  Found video:",
                    video["name"],
                    video["id"]
                )
