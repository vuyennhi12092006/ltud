import json
import os
import urllib.parse
import urllib.request

import requests

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
DRIVE_SCOPES = ["https://www.googleapis.com/auth/drive.file"]


def load_config():
    if not os.path.exists(CONFIG_PATH):
        return {}
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


def _resolve_path(path):
    if not path:
        return path
    if os.path.isabs(path):
        return path
    return os.path.join(BASE_DIR, path)


def shorten_url(url):
    api = (
        "https://tinyurl.com/api-create.php?url="
        + urllib.parse.quote(url, safe="")
    )
    with urllib.request.urlopen(api, timeout=15) as resp:
        return resp.read().decode("utf-8").strip()


def upload_to_imgur(file_path, client_id):
    headers = {"Authorization": f"Client-ID {client_id}"}
    is_video = file_path.lower().endswith((".mp4", ".avi", ".mov", ".webm"))

    with open(file_path, "rb") as f:
        if is_video:
            resp = requests.post(
                "https://api.imgur.com/3/upload",
                headers=headers,
                files={"video": f},
                timeout=120,
            )
        else:
            resp = requests.post(
                "https://api.imgur.com/3/image",
                headers=headers,
                files={"image": f},
                timeout=60,
            )

    resp.raise_for_status()
    data = resp.json()["data"]
    return data.get("link") or data.get("gif") or data.get("mp4")


def upload_to_s3(file_path, aws_cfg):
    import boto3

    bucket = aws_cfg["bucket"]
    region = aws_cfg.get("region", "ap-southeast-1")
    key = os.path.basename(file_path)

    client = boto3.client(
        "s3",
        aws_access_key_id=aws_cfg["access_key"],
        aws_secret_access_key=aws_cfg["secret_key"],
        region_name=region,
    )
    client.upload_file(file_path, bucket, key)
    return client.generate_presigned_url(
        "get_object",
        Params={"Bucket": bucket, "Key": key},
        ExpiresIn=7 * 24 * 3600,
    )


def _get_oauth_credentials(drive_cfg):
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow

    client_secrets = _resolve_path(
        drive_cfg.get("client_secrets_file", "credentials.json")
    )
    token_file = _resolve_path(drive_cfg.get("token_file", "token.json"))

    if not os.path.exists(client_secrets):
        raise FileNotFoundError(
            f"Không tìm thấy {client_secrets}\n"
            "Tạo OAuth Client ID (Desktop app) trên Google Cloud Console, "
            "tải JSON và đặt tên credentials.json trong thư mục camera/."
        )

    creds = None
    if os.path.exists(token_file):
        creds = Credentials.from_authorized_user_file(token_file, DRIVE_SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                client_secrets, DRIVE_SCOPES
            )
            creds = flow.run_local_server(port=0, prompt="consent")
        with open(token_file, "w", encoding="utf-8") as f:
            f.write(creds.to_json())

    return creds


def _get_service_account_credentials(drive_cfg):
    from google.oauth2 import service_account

    creds_file = _resolve_path(drive_cfg.get("credentials_file", "service_account.json"))
    if not os.path.exists(creds_file):
        raise FileNotFoundError(
            f"Không tìm thấy file credentials: {creds_file}\n"
            "Hãy tải JSON Service Account từ Google Cloud Console."
        )

    return service_account.Credentials.from_service_account_file(
        creds_file, scopes=DRIVE_SCOPES
    )


def _drive_create_and_share(service, file_path, folder_id):
    from googleapiclient.errors import HttpError
    from googleapiclient.http import MediaFileUpload

    metadata = {"name": os.path.basename(file_path)}
    if folder_id:
        metadata["parents"] = [folder_id]

    media = MediaFileUpload(file_path, resumable=True)
    try:
        created = (
            service.files()
            .create(body=metadata, media_body=media, fields="id,webViewLink")
            .execute()
        )
    except HttpError as err:
        detail = err.reason or str(err)
        if err.resp.status == 403 and "storage quota" in detail.lower():
            raise RuntimeError(
                "Service Account không có dung lượng Drive riêng.\n"
                "Với Gmail cá nhân, đổi config.json:\n"
                '  "auth_type": "oauth"\n'
                "và dùng credentials.json (OAuth Desktop app).\n"
                f"Chi tiết: {detail}"
            ) from err
        if err.resp.status == 403:
            raise RuntimeError(
                "Google Drive từ chối upload (403).\n"
                "Kiểm tra: đã bật Drive API, folder_id đúng, "
                "tài khoản có quyền ghi vào folder.\n"
                f"Chi tiết: {detail}"
            ) from err
        raise RuntimeError(f"Lỗi Google Drive: {detail}") from err

    file_id = created["id"]
    try:
        service.permissions().create(
            fileId=file_id,
            body={"type": "anyone", "role": "reader"},
        ).execute()
    except HttpError:
        pass

    return created.get("webViewLink") or f"https://drive.google.com/file/d/{file_id}/view"


def upload_to_google_drive(file_path, drive_cfg):
    from googleapiclient.discovery import build

    auth_type = drive_cfg.get("auth_type", "oauth").lower()
    folder_id = drive_cfg.get("folder_id", "").strip()

    if auth_type == "service_account":
        credentials = _get_service_account_credentials(drive_cfg)
    elif auth_type == "oauth":
        credentials = _get_oauth_credentials(drive_cfg)
    else:
        raise ValueError(
            f"auth_type không hợp lệ: {auth_type}. Dùng 'oauth' hoặc 'service_account'."
        )

    service = build("drive", "v3", credentials=credentials, cache_discovery=False)
    return _drive_create_and_share(service, file_path, folder_id)


def upload_file(file_path, provider=None):
    cfg = load_config()
    provider = provider or cfg.get("upload_provider", "imgur")

    if provider == "imgur":
        client_id = cfg.get("imgur_client_id", "").strip()
        if not client_id:
            raise ValueError("Thiếu imgur_client_id trong config.json")
        url = upload_to_imgur(file_path, client_id)

    elif provider == "s3":
        aws_cfg = cfg.get("aws", {})
        required = ("access_key", "secret_key", "bucket")
        if not all(aws_cfg.get(k) for k in required):
            raise ValueError("Thiếu cấu hình AWS S3 trong config.json")
        url = upload_to_s3(file_path, aws_cfg)

    elif provider == "google_drive":
        drive_cfg = cfg.get("google_drive", {})
        url = upload_to_google_drive(file_path, drive_cfg)

    else:
        raise ValueError(f"Nhà cung cấp không hỗ trợ: {provider}")

    if cfg.get("shorten_links", True):
        try:
            return shorten_url(url)
        except Exception:
            return url
    return url
