import os
import uuid

from flask import Flask, request, jsonify
from flask.cli import load_dotenv
from openai import OpenAI
from supabase import create_client, Client
from werkzeug.exceptions import RequestEntityTooLarge
from werkzeug.utils import secure_filename

load_dotenv()

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024
app.config["UPLOAD_FOLDER"] = "/tmp"

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
SUPABASE_BUCKET = os.getenv("SUPABASE_BUCKET")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

if not SUPABASE_BUCKET or not SUPABASE_KEY or not SUPABASE_URL or not OPENAI_API_KEY:
    raise ValueError()

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
client = OpenAI(api_key=OPENAI_API_KEY)

ALLOWED_EXTENSIONS = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
}


@app.route("/extract", methods=["POST"])
def extract_file_info():
    files = request.files.getlist("files")
    saved_paths = []

    if not files:
        return error_message("No files provided", 400)

    for file in files:
        file_ext = os.path.splitext(file.filename)[1].lower()

        if file_ext not in ALLOWED_EXTENSIONS:
            return error_message("File type not allowed", 400)

        filename = secure_filename(file.filename)
        path = os.path.join(app.config["UPLOAD_FOLDER"], filename)
        file.save(path)
        saved_paths.append(path)

    extracted_text = extract_data_open_ai(saved_paths)

    if not extracted_text:
        return error_message("Failed to extract text", 500)

    return jsonify({"extracted_text": extracted_text}), 200


@app.route('/upload', methods=["POST"])
def upload_file():
    file = request.files.get("file")

    if not file:
        return jsonify({"error": "No files provided"}), 400

    filename = secure_filename(file.filename)
    unique_name = f"{uuid.uuid4()}_{filename}"

    file_bytes = file.read()

    try:
        res = supabase.storage.from_(SUPABASE_BUCKET).upload(unique_name, file_bytes)

        if res is None:
            return jsonify({"error": "Upload failed"}), 500

        signed_url = supabase.storage.from_(SUPABASE_BUCKET).create_signed_url(unique_name, 3600)

        return jsonify({
            "message": "Upload successful",
            "filename": unique_name,
            "url": signed_url['signedURL']
        }), 200

    except Exception as e:
        return error_message(str(e), 500)


@app.errorhandler(RequestEntityTooLarge)
def handle_large_file(e):
    return error_message("File too large. Max file size is 10 MB", 413)


def extract_data_open_ai(saved_paths):
    try:
        file_objects = []
        for path in saved_paths:
            with open(path, 'rb') as file:
                uploaded_file = client.files.create(
                    file=file,
                    purpose='assistants'
                )
                file_objects.append(uploaded_file)

        assistant = client.beta.assistants.create(
            name="Document Analyzer",
            instructions=(
                "You are a document analyzer. Analyze, extract, and combine all information "
                "from the provided files, preserving order. Return the result as plain text."
            ),
            model="gpt-4o",
            tools=[{"type": "file_search"}],
        )

        thread = client.beta.threads.create(
            messages=[
                {
                    "role": "user",
                    "content": "Please combine and analyze these uploaded documents.",
                    "attachments": [{"file_id": f.id, "tools": [{"type": "file_search"}]} for f in file_objects]
                }
            ]
        )

        client.beta.threads.runs.create_and_poll(
            thread_id=thread.id,
            assistant_id=assistant.id,
        )

        messages = client.beta.threads.messages.list(thread_id=thread.id)

        return messages.data[0].content[0].text.value
    except Exception as e:
        print("Error processing files: ", str(e))

        return None
    finally:
        for p in saved_paths:
            try:
                os.remove(p)
            except Exception as e:
                print("Error deleting file: ", str(e))


def error_message(message, status_code):
    return jsonify({"error": message}), status_code


if __name__ == '__main__':
    app.run()
