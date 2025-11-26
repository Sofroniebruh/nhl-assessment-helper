import os
import uuid
import zipfile
import tempfile
from flask import Flask, request, jsonify, send_file
from flask.cli import load_dotenv
from supabase import create_client
from werkzeug.exceptions import RequestEntityTooLarge
from werkzeug.utils import secure_filename

load_dotenv()

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY") 
SUPABASE_BUCKET = os.getenv("SUPABASE_BUCKET")

if not all([SUPABASE_URL, SUPABASE_KEY, SUPABASE_BUCKET]):
    print("Warning: Supabase environment variables not set - upload functionality disabled")
    supabase = None
else:
    try:
        supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
    except Exception as e:
        print(f"Warning: Could not connect to Supabase: {e}")
        supabase = None


def merge_docx_files(file_paths):
    if len(file_paths) < 2:
        raise ValueError("Need at least 2 files to merge")
    
    base_path = file_paths[0]
    output_path = f"/tmp/merged_{uuid.uuid4()}.docx"
    
    with tempfile.TemporaryDirectory() as temp_dir:
        base_dir = os.path.join(temp_dir, "base")
        with zipfile.ZipFile(base_path, 'r') as zip_ref:
            zip_ref.extractall(base_dir)
        
        doc_xml_path = os.path.join(base_dir, "word", "document.xml")
        with open(doc_xml_path, 'r', encoding='utf-8') as f:
            base_xml = f.read()
        
        body_end = base_xml.rfind('</w:body>')
        if body_end == -1:
            raise ValueError("Invalid document structure")
        
        merged_parts = [base_xml[:body_end]]
        
        for additional_path in file_paths[1:]:
            additional_dir = os.path.join(temp_dir, f"doc_{uuid.uuid4()}")
            with zipfile.ZipFile(additional_path, 'r') as zip_ref:
                zip_ref.extractall(additional_dir)
            
            additional_xml_path = os.path.join(additional_dir, "word", "document.xml")
            with open(additional_xml_path, 'r', encoding='utf-8') as f:
                additional_xml = f.read()
            
            body_start = additional_xml.find('<w:body>')
            body_end_pos = additional_xml.rfind('</w:body>')
            
            if body_start != -1 and body_end_pos != -1:
                body_content = additional_xml[body_start + 8:body_end_pos]
                
                last_sect = body_content.rfind('<w:sectPr')
                if last_sect != -1 and body_content[last_sect:].strip().endswith('</w:sectPr>'):
                    body_content = body_content[:last_sect].rstrip()
                
                page_break = '<w:p><w:r><w:br w:type="page"/></w:r></w:p>'
                merged_parts.extend([page_break, body_content])
        
        merged_parts.append(base_xml[body_end:])
        
        merged_xml = ''.join(merged_parts)
        with open(doc_xml_path, 'w', encoding='utf-8') as f:
            f.write(merged_xml)
        
        with zipfile.ZipFile(output_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
            for root, dirs, files in os.walk(base_dir):
                for file in files:
                    file_path = os.path.join(root, file)
                    arcname = os.path.relpath(file_path, base_dir)
                    zipf.write(file_path, arcname)
    
    return output_path


@app.route("/merge", methods=["POST"])
def merge_documents():
    files = request.files.getlist("files")
    
    if len(files) < 2:
        return error_response("Need at least 2 .docx files", 400)
    
    temp_files = []
    try:
        for file in files:
            if not file.filename.lower().endswith('.docx'):
                return error_response("Only .docx files allowed", 400)
            
            temp_path = f"/tmp/{uuid.uuid4()}_{secure_filename(file.filename)}"
            file.save(temp_path)
            temp_files.append(temp_path)
        
        merged_path = merge_docx_files(temp_files)
        upload_to_supabase = request.form.get("upload_to_supabase", "false").lower() == "true"
        
        if upload_to_supabase:
            if not supabase:
                return error_response("Supabase not configured", 500)
            
            filename = f"{uuid.uuid4()}_merged.docx"
            with open(merged_path, 'rb') as f:
                supabase.storage.from_(SUPABASE_BUCKET).upload(filename, f.read())
            
            signed_url = supabase.storage.from_(SUPABASE_BUCKET).create_signed_url(filename, 3600)
            return jsonify({
                "success": True,
                "url": signed_url['signedURL'],
                "filename": filename
            })
        else:
            return send_file(merged_path, as_attachment=True, download_name="merged.docx")
            
    except Exception as e:
        return error_response(str(e), 500)
    finally:
        for path in temp_files + [merged_path if 'merged_path' in locals() else None]:
            if path and os.path.exists(path):
                os.remove(path)


@app.route('/upload', methods=["POST"])
def upload_file():
    if not supabase:
        return error_response("Supabase not configured", 500)
        
    file = request.files.get("file")
    if not file:
        return error_response("No file provided", 400)
    
    filename = f"{uuid.uuid4()}_{secure_filename(file.filename)}"
    
    try:
        supabase.storage.from_(SUPABASE_BUCKET).upload(filename, file.read())
        signed_url = supabase.storage.from_(SUPABASE_BUCKET).create_signed_url(filename, 3600)
        return jsonify({
            "success": True,
            "filename": filename,
            "url": signed_url['signedURL']
        })
    except Exception as e:
        return error_response(str(e), 500)


@app.route('/health')
def health():
    return jsonify({"status": "healthy"})


@app.errorhandler(RequestEntityTooLarge)
def handle_large_file(e):
    return error_response("File too large (max 10MB)", 413)


def error_response(message, status_code):
    return jsonify({"success": False, "error": message}), status_code


if __name__ == '__main__':
    app.run(port=5000, host="localhost", debug=True)