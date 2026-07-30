from google.oauth2 import service_account
from googleapiclient.discovery import build


SCOPES = [
    "https://www.googleapis.com/auth/drive"
]


creds = service_account.Credentials.from_service_account_file(
    "service_account.json",
    scopes=SCOPES
)


drive = build(
    "drive",
    "v3",
    credentials=creds
)


results = drive.files().list(
    q="name='ReoLinkSecurityCamera'",
    fields="files(id, name)"
).execute()


files = results.get("files", [])


if not files:
    print("Folder not found")
else:
    for f in files:
        print("Found folder:")
        print(f["name"])
        print("ID:", f["id"])