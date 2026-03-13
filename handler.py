import os
import io
import json
import time
import uuid
import base64
import mimetypes
import requests
import runpod
import firebase_admin

from PIL import Image
from firebase_admin import credentials, storage

# -----------------------------
# Firebase setup
# -----------------------------
if not firebase_admin._apps:
    service_account_json = os.environ.get("FIREBASE_SERVICE_ACCOUNT")
    if not service_account_json:
        raise EnvironmentError("FIREBASE_SERVICE_ACCOUNT environment variable is not set")

    key_data = json.loads(service_account_json)
    print("FIREBASE PROJECT ID:", key_data.get("project_id"))
    print("FIREBASE CLIENT EMAIL:", key_data.get("client_email"))
    print("FIREBASE PRIVATE KEY START:", str(key_data.get("private_key", ""))[:40])

    cred = credentials.Certificate(key_data)
    firebase_admin.initialize_app(cred, {
        "storageBucket": "impulse-upscaler.firebasestorage.app"
    })

bucket = storage.bucket()

# -----------------------------
# Config
# -----------------------------
COMFY_URL = "http://127.0.0.1:8188"
COMFY_INPUT_DIR = "/comfyui/input"
COMFY_OUTPUT_DIR = "/comfyui/output"
WORKFLOW_PATH = "/app/workflow_api.json"

# -----------------------------
# Helpers
# -----------------------------
def clean_base64(data: str) -> str:
    if "," in data:
        return data.split(",", 1)[1]
    return data

def save_input_image(image_b64: str, filename: str = "input_image.png") -> str:
    os.makedirs(COMFY_INPUT_DIR, exist_ok=True)

    image_b64 = clean_base64(image_b64)
    image_bytes = base64.b64decode(image_b64)

    image = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    input_path = os.path.join(COMFY_INPUT_DIR, filename)
    image.save(input_path, format="PNG")

    return input_path

def load_workflow():
    with open(WORKFLOW_PATH, "r", encoding="utf-8") as f:
        return json.load(f)

def queue_prompt(prompt):
    response = requests.post(f"{COMFY_URL}/prompt", json={"prompt": prompt}, timeout=30)
    response.raise_for_status()
    return response.json()

def wait_for_completion(prompt_id, timeout=300):
    start = time.time()

    while time.time() - start < timeout:
        response = requests.get(f"{COMFY_URL}/history/{prompt_id}", timeout=30)
        response.raise_for_status()
        history = response.json()

        if prompt_id in history:
            return history[prompt_id]

        time.sleep(2)

    raise TimeoutError("ComfyUI generation timed out")

def find_latest_output(prefix="ComfyUI"):
    if not os.path.exists(COMFY_OUTPUT_DIR):
        raise FileNotFoundError("ComfyUI output directory does not exist")

    files = [
        os.path.join(COMFY_OUTPUT_DIR, f)
        for f in os.listdir(COMFY_OUTPUT_DIR)
        if f.startswith(prefix) and f.lower().endswith((".png", ".jpg", ".jpeg", ".webp"))
    ]

    if not files:
        raise FileNotFoundError("No output image found in ComfyUI output directory")

    files.sort(key=os.path.getmtime, reverse=True)
    return files[0]

def upload_to_firebase(local_file_path):
    ext = os.path.splitext(local_file_path)[1].lower() or ".png"
    unique_name = f"outputs/upscaled-{int(time.time())}-{uuid.uuid4().hex}{ext}"

    blob = bucket.blob(unique_name)

    content_type = mimetypes.guess_type(local_file_path)[0] or "image/png"
    token = str(uuid.uuid4())

    blob.metadata = {
        "firebaseStorageDownloadTokens": token
    }

    blob.upload_from_filename(local_file_path, content_type=content_type)

    image_url = (
        f"https://firebasestorage.googleapis.com/v0/b/{bucket.name}/o/"
        f"{requests.utils.quote(unique_name, safe='')}"
        f"?alt=media&token={token}"
    )

    return image_url, unique_name

# -----------------------------
# Main handler
# -----------------------------
def handler(job):
    try:
        job_input = job.get("input", {})
        image_b64 = job_input.get("imageBase64") or job_input.get("image")

        if not image_b64:
            return {
                "success": False,
                "error": "Missing imageBase64 in input"
            }

        print("STEP 1: Received job")
        print("STEP 2: Saving input image")
        input_path = save_input_image(image_b64, "input_image.png")
        print(f"Saved input image to: {input_path}")

        print("STEP 3: Loading workflow")
        workflow = load_workflow()

        print("STEP 4: Queueing prompt")
        queued = queue_prompt(workflow)
        prompt_id = queued.get("prompt_id")

        if not prompt_id:
            return {
                "success": False,
                "error": "No prompt_id returned from ComfyUI"
            }

        print(f"Prompt ID: {prompt_id}")

        print("STEP 5: Waiting for completion")
        wait_for_completion(prompt_id, timeout=300)

        print("STEP 6: Finding latest output")
        output_path = find_latest_output("ComfyUI")
        print(f"Output file found: {output_path}")

        print("STEP 7: Uploading to Firebase")
        image_url, storage_path = upload_to_firebase(output_path)
        print(f"Uploaded to Firebase: {image_url}")

        return {
            "success": True,
            "image_url": image_url,
            "storage_path": storage_path
        }

    except Exception as e:
        print("ERROR:", str(e))
        return {
            "success": False,
            "error": str(e)
        }


runpod.serverless.start({"handler": handler})


