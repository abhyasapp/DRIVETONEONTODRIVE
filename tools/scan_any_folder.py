"""
scan_any_folder.py
------------------
Lists everything inside a Drive folder (recursive).
"""

import sys
from google.oauth2 import service_account
from googleapiclient.discovery import build

SERVICE_ACCOUNT_FILE = "service-account.json"
SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]


def client():
    creds = service_account.Credentials.from_service_account_file(
        SERVICE_ACCOUNT_FILE, scopes=SCOPES)
    return build("drive", "v3", credentials=creds)


def list_children(drive, folder_id):
    items, token = [], None
    while True:
        r = drive.files().list(
            q=f"'{folder_id}' in parents and trashed = false",
            fields="nextPageToken, files(id, name, mimeType)",
            pageSize=200, pageToken=token,
            supportsAllDrives=True, includeItemsFromAllDrives=True,
        ).execute()
        items.extend(r.get("files", []))
        token = r.get("nextPageToken")
        if not token:
            break
    return items


def walk(drive, folder_id, indent=0):
    for item in list_children(drive, folder_id):
        is_folder = item["mimeType"] == "application/vnd.google-apps.folder"
        marker = "[DIR]" if is_folder else "     "
        print("  " * indent + f"{marker} {item['name']}  (id: {item['id']})")
        if is_folder:
            walk(drive, item["id"], indent + 1)


def main():
    root = sys.argv[1] if len(sys.argv) > 1 else input("Folder ID: ").strip()
    drive = client()
    print(f"\nListing: {root}\n")
    walk(drive, root)
    print("\nDone.")


if __name__ == "__main__":
    main()