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
    print("USING SERVICE ACCOUNT:", key_data.get("client_email"))
    print("FIREBASE PROJECT ID:", key_data.get("project_id"))
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

def get_image_dimensions(image_path: str):
    with Image.open(image_path) as img:
        return img.size  # (width, height)

def resize_image_to_2x(original_path: str, upscaled_path: str) -> str:
    """Resize 4x upscaled image down to 2x of original dimensions"""
    with Image.open(original_path) as orig:
        orig_w, orig_h = orig.size
        target_w = orig_w * 2
        target_h = orig_h * 2

    with Image.open(upscaled_path) as img:
        resized = img.resize((target_w, target_h), Image.LANCZOS)
        output_path = upscaled_path.replace(".png", "_2x.png")
        resized.save(output_path, format="PNG")

    print(f"Resized from 4x to 2x: {target_w}x{target_h}")
    return output_path

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

def run_4x_upscale(input_filename: str) -> str:
    """Run one 4x upscale pass through ComfyUI, returns output path"""
    workflow = load_workflow()

    # Update workflow to use the correct input filename
    for node_id, node in workflow.items():
        if node.get("class_type") == "LoadImage":
            node["inputs"]["image"] = input_filename

    queued = queue_prompt(workflow)
    prompt_id = queued.get("prompt_id")
    if not prompt_id:
        raise ValueError("No prompt_id returned from ComfyUI")

    print(f"Prompt ID: {prompt_id}")
    wait_for_completion(prompt_id, timeout=300)
    return find_latest_output("ComfyUI")

def copy_output_to_input(output_path: str, new_filename: str) -> str:
    """Copy an output image into ComfyUI input folder for second pass"""
    os.makedirs(COMFY_INPUT_DIR, exist_ok=True)
    dest_path = os.path.join(COMFY_INPUT_DIR, new_filename)
    with Image.open(output_path) as img:
        img.save(dest_path, format="PNG")
    print(f"Copied output to input for second pass: {dest_path}")
    return dest_path

def upload_to_firebase(local_file_path: str, scale: int):
    ext = os.path.splitext(local_file_path)[1].lower() or ".png"
    unique_name = f"outputs/upscaled-{scale}x-{int(time.time())}-{uuid.uuid4().hex}{ext}"

    blob = bucket.blob(unique_name)
    content_type = mimetypes.guess_type(local_file_path)[0] or "image/png"
    token = str(uuid.uuid4())

    blob.metadata = {"firebaseStorageDownloadTokens": token}
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
        scale = int(job_input.get("scale", 4))  # default 4x

        if scale not in [2, 4, 8]:
            return {"success": False, "error": f"Invalid scale: {scale}. Must be 2, 4, or 8."}

        if not image_b64:
            return {"success": False, "error": "Missing imageBase64 in input"}

        print(f"STEP 1: Received job — scale={scale}x")

        print("STEP 2: Saving input image")
        input_path = save_input_image(image_b64, "input_image.png")
        print(f"Saved input image to: {input_path}")

        if scale == 2:
            # Run 4x then resize down to 2x
            print("STEP 3: Running 4x upscale (will resize to 2x after)")
            output_path = run_4x_upscale("input_image.png")
            print(f"4x output: {output_path}")

            print("STEP 4: Resizing 4x result to 2x")
            final_path = resize_image_to_2x(input_path, output_path)

        elif scale == 4:
            # Standard 4x
            print("STEP 3: Running 4x upscale")
            final_path = run_4x_upscale("input_image.png")
            print(f"4x output: {final_path}")

        elif scale == 8:
            # Run 4x twice
            print("STEP 3: Running first 4x upscale pass")
            first_pass_path = run_4x_upscale("input_image.png")
            print(f"First pass output: {first_pass_path}")

            print("STEP 4: Copying first pass output to input for second pass")
            copy_output_to_input(first_pass_path, "input_image_pass2.png")

            print("STEP 5: Running second 4x upscale pass")
            final_path = run_4x_upscale("input_image_pass2.png")
            print(f"Second pass output: {final_path}")

        print(f"STEP FINAL: Uploading {scale}x result to Firebase")
        image_url, storage_path = upload_to_firebase(final_path, scale)
        print(f"Uploaded to Firebase: {image_url}")

        return {
            "success": True,
            "image_url": image_url,
            "storage_path": storage_path,
            "scale": scale
        }

    except Exception as e:
        print("ERROR:", str(e))
        return {"success": False, "error": str(e)}


runpod.serverless.start({"handler": handler})
