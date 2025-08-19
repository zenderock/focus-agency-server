from flask import Flask, render_template, request, redirect, url_for, send_from_directory, abort, send_file, jsonify
from flask_cors import CORS
import os
import shutil
import subprocess
import time
import uuid
import hashlib
import hmac
import jwt
from datetime import datetime, timedelta
from werkzeug.utils import secure_filename
from celery import Celery
from functools import wraps
import re
import requests
from pymesomb.operations import PaymentOperation
from pymesomb.utils import RandomGenerator

FOCUST_ALLOWED_ORIGINS = [
    "https://focustagency.com",
    "https://trainer.focustagency.com",
    "https://learner.focustagency.com",
    "http://localhost:3000"
]

app = Flask(__name__)
CORS(app, 
     resources={r"/*": {"origins": FOCUST_ALLOWED_ORIGINS}},
     allow_headers=["Content-Type", "Authorization"],
     methods=["GET", "POST", "OPTIONS"]
)

# Configuration
UPLOAD_FOLDER = 'uploads'
HLS_FOLDER = 'hls'
PRESENTATION_VIDEOS_FOLDER = 'presentation_videos'
ORIGINALS_FOLDER = 'originals'

ALLOWED_EXTENSIONS = {'mp4', 'avi', 'mov', 'wmv', 'flv'}
SECRET_KEY = 'ntA4{Q6NLb?fRgs|]U^MV.u@d,m44IF(AFLm]-4=P-[gC5<u8_PvwYt-*.+Rgop_[www.zenderock.me]'
TOKEN_EXPIRY = 3600
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['MAX_CONTENT_LENGTH'] = 100 * 1024 * 1024
app.config['SECRET_KEY'] = SECRET_KEY
app.config['MESOMB_APP_KEY']="914d0452f4c8b7cf06e4c395d1c401012613bcb2"
app.config['MESOMB_API_KEY']="7a5a5445-76ca-4631-8a2b-307a1561acac"
app.config['MESOMB_API_SECRET']="642ffb65-912a-402e-adfc-fccb41a1107c"

def _get_token_from_request():
    auth_header = request.headers.get('Authorization', '')
    if auth_header.startswith('Bearer '):
        return auth_header.split(' ', 1)[1].strip()
    return request.args.get('token')

def make_celery(app):
    celery = Celery(
        app.import_name,
        backend='redis://localhost:6379/0',
        broker='redis://localhost:6379/0'
    )
    celery.conf.update(
        app.config,
        broker_connection_retry_on_startup=True  
    )
    return celery

celery = make_celery(app)

# Token verification decorator
def token_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        token = _get_token_from_request()
        
        if not token:
            return jsonify({'message': 'Token is missing'}), 403
            
        try:
            # Verify the token
            data = jwt.decode(token, app.config['SECRET_KEY'], algorithms=['HS256'])
            print(data)
            print(kwargs)
            # Check if token is for the correct user and video
            if 'user_id' in kwargs and data.get('user_id') != kwargs['user_id']:
                raise Exception("Invalid user")
                
            if 'filename' in kwargs and data.get('filename') != kwargs['filename']:
                raise Exception("Invalid file")
                
            # Check referrer: required for web tokens (platform != mobile)
            if (data.get('platform') != 'mobile'):
                if request.referrer:
                    allowed_referrers = [
                        r'https://focustagency\.com',
                        r'https://trainer\.focustagency\.com',
                        r'https://learner\.focustagency\.com',
                        r'http://localhost:3000'
                    ]
                    referrer_allowed = False
                    for pattern in allowed_referrers:
                        if re.match(pattern, request.referrer):
                            referrer_allowed = True
                            break
                    if not referrer_allowed:
                        raise Exception("Invalid referrer")
                else:
                    raise Exception("No referrer")
                
        except Exception as e:
            print(f"Token verification failed: {str(e)}")
            return jsonify({'message': 'Token is invalid or expired'}), 403
            
        return f(*args, **kwargs)
    return decorated

# Token verification decorator for mobile apps (no referrer, requires platform=mobile)
def token_required_mobile(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        token = _get_token_from_request()
        if not token:
            return jsonify({'message': 'Token is missing'}), 403
        try:
            data = jwt.decode(token, app.config['SECRET_KEY'], algorithms=['HS256'])
            if data.get('platform') != 'mobile':
                raise Exception('Invalid platform')
            if 'user_id' in kwargs and data.get('user_id') != kwargs['user_id']:
                raise Exception('Invalid user')
            # video_id binding when present
            if 'video_id' in kwargs and data.get('video_id') and data.get('video_id') != kwargs['video_id']:
                raise Exception('Invalid video')
        except Exception as e:
            print(f"Mobile token verification failed: {str(e)}")
            return jsonify({'message': 'Token is invalid or expired'}), 403
        return f(*args, **kwargs)
    return decorated

# Token verification decorator for downloads (no referrer, requires action=download or platform=download)
def token_required_download(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        token = _get_token_from_request()
        if not token:
            return jsonify({'message': 'Token is missing'}), 403
        try:
            data = jwt.decode(token, app.config['SECRET_KEY'], algorithms=['HS256'])
            if not (data.get('action') == 'download' or data.get('platform') == 'download'):
                raise Exception('Invalid action')
            if 'user_id' in kwargs and data.get('user_id') != kwargs['user_id']:
                raise Exception('Invalid user')
            if 'filename' in kwargs and data.get('filename') != kwargs['filename']:
                raise Exception('Invalid file')
        except Exception as e:
            print(f"Download token verification failed: {str(e)}")
            return jsonify({'message': 'Token is invalid or expired'}), 403
        return f(*args, **kwargs)
    return decorated

@celery.task
def convert_to_hls(input_path: str, hls_dir: str, success_callback_url=None, error_callback_url=None, user_id=None, video_id=None, encryption_key=None):
    output_path = os.path.join(hls_dir, 'output.m3u8')
    key_path = os.path.join(hls_dir, 'enc.key')
    key_info_path = os.path.join(hls_dir, 'enc.keyinfo')
    
    if encryption_key is None:
        encryption_key = os.urandom(16)
    with open(key_path, 'wb') as key_file:
        key_file.write(encryption_key)
    
    # URL absolue pour la clé
    if not user_id:
        user_id = hls_dir.split(os.sep)[-2]
    if not video_id:
        video_id = hls_dir.split(os.sep)[-1]
    key_url = f"https://server.focustagency.com/hls/{user_id}/{video_id}/key"
    
    with open(key_info_path, 'w') as key_info_file:
        key_info_file.write(f"{key_url}\n{key_path}\n")
    
    cmd = [
        'ffmpeg',
        '-i', input_path,
        '-c:v', 'h264',
        '-c:a', 'aac',
        '-hls_time', '10',
        '-hls_list_size', '0',
        '-hls_key_info_file', key_info_path,
        '-hls_segment_filename', os.path.join(hls_dir, 'segment_%03d.ts'),
        output_path
    ]
    
    try:
        subprocess.run(cmd, check=True)
        os.remove(input_path)
        
        if success_callback_url:
            try:
                payload = {
                    'status': 'success',
                    'user_id': user_id,
                    'video_id': video_id,
                    'hls_path': f"/hls/{user_id}/{video_id}/output.m3u8",
                    'message': 'Conversion terminée avec succès'
                }
                requests.post(success_callback_url, json=payload, timeout=10, headers={"Content-Type": "application/json", "Authorization": "Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InBqdHdubmVyaWFqdXBwZXp1cXFzIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NDAzOTk4NDcsImV4cCI6MjA1NTk3NTg0N30.LvqiUfjsRlXjOUe-s9ODhyh33GskNbeEsoC2TapNC1s"})
            except Exception as callback_error:
                app.logger.error(f"Erreur callback succès: {callback_error}")
                
    except subprocess.CalledProcessError as e:
        app.logger.error(f"Erreur FFmpeg: {e}")
        
        if error_callback_url:
            try:
                payload = {
                    'status': 'error',
                    'user_id': user_id,
                    'video_id': video_id,
                    'error': str(e),
                    'message': 'Échec de la conversion vidéo'
                }
                requests.post(error_callback_url, json=payload, timeout=10, headers={"Content-Type": "application/json", "Authorization": "Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InBqdHdubmVyaWFqdXBwZXp1cXFzIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NDAzOTk4NDcsImV4cCI6MjA1NTk3NTg0N30.LvqiUfjsRlXjOUe-s9ODhyh33GskNbeEsoC2TapNC1s"})
            except Exception as callback_error:
                app.logger.error(f"Erreur callback échec: {callback_error}")
        
        raise e
def allowed_file(filename):
    return '.' in filename and \
           filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

@app.route('/upload', methods=['OPTIONS'])
def upload_preflight():
    return jsonify({'message': 'Preflight OK'}), 200

@app.route('/upload', methods=['POST'])
def upload_video():
    if 'video' not in request.files:
        return jsonify({'message': 'Aucune vidéo uploadée'}), 400
    file = request.files['video']
    app.logger.info('Fichier reçu: %s, type: %s', file.filename, file.content_type)
    user_id = request.form.get('user_id')
    if file.filename == '':
        return jsonify({'message': 'Aucun fichier sélectionné'}), 400
    if not user_id:
        return jsonify({'message': 'ID Utilisateur manquant'}), 400
    if file and allowed_file(file.filename):
        filename = secure_filename(file.filename)
        user_folder = os.path.join(app.config['UPLOAD_FOLDER'], user_id)
        os.makedirs(user_folder, exist_ok=True)
        file_path = os.path.join(user_folder, filename)
        file.save(file_path)

        # Conserver une copie originale pour le téléchargement
        originals_user_folder = os.path.join(ORIGINALS_FOLDER, user_id)
        os.makedirs(originals_user_folder, exist_ok=True)
        original_path = os.path.join(originals_user_folder, filename)
        try:
            shutil.copy2(file_path, original_path)
        except Exception as e:
            app.logger.error(f"Échec copie original: {e}")

        # Définir le dossier HLS
        video_id = filename.split('.')[0]  # Utiliser le nom du fichier sans extension comme video_id
        hls_dir = os.path.join(HLS_FOLDER, user_id, video_id)
        os.makedirs(hls_dir, exist_ok=True)
        
        success_callback_url = "https://pjtwnneriajuppezuqqs.supabase.co/functions/v1/update-lesson-conversion-status"
        error_callback_url = "https://pjtwnneriajuppezuqqs.supabase.co/functions/v1/update-lesson-conversion-status"
        
        # Lancer la tâche asynchrone
        task = convert_to_hls.delay(
            file_path, 
            hls_dir, 
            success_callback_url, 
            error_callback_url, 
            user_id, 
            video_id
        )

        # Renvoyer une réponse JSON
        return jsonify({
            'message': 'Conversion en cours',
            'task_id': task.id,
            'video_id': video_id,
            'hls_path': f"/hls/{user_id}/{video_id}/output.m3u8"
        }), 202


# Generate a short-lived token for video access
def generate_video_token(user_id, filename, duration=TOKEN_EXPIRY, platform='web', video_id=None):
    payload = {
        'user_id': user_id,
        'filename': filename,
        'exp': datetime.utcnow() + timedelta(seconds=duration),
        'iat': datetime.utcnow(),
        'jti': str(uuid.uuid4()),
        'platform': platform
    }
    if video_id:
        payload['video_id'] = video_id
    return jwt.encode(payload, app.config['SECRET_KEY'], algorithm='HS256')

# Generate a short-lived token for download access
def generate_download_token(user_id, filename, duration=900):
    payload = {
        'user_id': user_id,
        'filename': filename,
        'exp': datetime.utcnow() + timedelta(seconds=duration),
        'iat': datetime.utcnow(),
        'jti': str(uuid.uuid4()),
        'action': 'download',
        'platform': 'download'
    }
    return jwt.encode(payload, app.config['SECRET_KEY'], algorithm='HS256')

@app.route('/api/get-video-token/<user_id>/<filename>')
def get_video_token(user_id, filename):
    token = generate_video_token(user_id, filename)
    return jsonify({
        'token': token,
        'expires_in': TOKEN_EXPIRY
    })

@app.route('/api/get-video-token/<user_id>/<filename>', methods=['OPTIONS'])
def preflight(user_id, filename):
    return jsonify({'message': 'Preflight OK'}), 200


# Endpoint pour émettre un token mobile (platform=mobile)
@app.route('/api/get-video-token/mobile/<user_id>/<filename>/<video_id>')
def get_mobile_video_token(user_id, filename, video_id):
    try:
        ttl = int(request.args.get('ttl', TOKEN_EXPIRY))
    except Exception:
        ttl = TOKEN_EXPIRY
    token = generate_video_token(user_id, filename, duration=ttl, platform='mobile', video_id=video_id)
    return jsonify({
        'token': token,
        'expires_in': ttl,
        'playlist_url': f"https://server.focustagency.com/mobile/hls/{user_id}/{video_id}/output.m3u8"
    })

@app.route('/api/get-video-token/mobile/<user_id>/<filename>/<video_id>', methods=['OPTIONS'])
def preflight_mobile(user_id, filename, video_id):
    return jsonify({'message': 'Preflight OK'}), 200


# Endpoint pour émettre un token de téléchargement
@app.route('/api/get-download-token/<user_id>/<filename>')
def get_download_token(user_id, filename):
    try:
        ttl = int(request.args.get('ttl', 900))
    except Exception:
        ttl = 900
    token = generate_download_token(user_id, filename, duration=ttl)
    return jsonify({
        'token': token,
        'expires_in': ttl,
        'download_url': f"https://server.focustagency.com/api/download/{user_id}/{filename}"
    })

@app.route('/api/get-download-token/<user_id>/<filename>', methods=['OPTIONS'])
def preflight_download_token(user_id, filename):
    return jsonify({'message': 'Preflight OK'}), 200


# Endpoint pour télécharger la vidéo originale (force attachment)
@app.route('/api/download/<user_id>/<filename>')
@token_required_download
def download_video(user_id, filename):
    user_folder = os.path.join(ORIGINALS_FOLDER, user_id)
    file_path = os.path.join(user_folder, filename)
    if not os.path.exists(file_path):
        return jsonify({'message': 'File not found'}), 404
    try:
        response = send_file(file_path, as_attachment=True)
        response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
        response.headers['Pragma'] = 'no-cache'
        response.headers['Expires'] = '0'
        return response
    except Exception as e:
        app.logger.error(f"Download failed: {e}")
        return jsonify({'message': 'Internal server error'}), 500


@app.route('/videos-user/<user_id>/<filename>')
@token_required
def serve_video(user_id, filename):
    user_folder = os.path.join(app.config['UPLOAD_FOLDER'], user_id)
    file_path = os.path.join(user_folder, filename)
    
    if not os.path.exists(file_path):
        abort(404)
    
    # Add Cache-Control headers to prevent caching
    response = send_file(file_path, conditional=True)
    response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    
    # Add custom headers to make downloading harder
    response.headers['Content-Disposition'] = 'inline'
    
    return response

@app.route('/hls/<user_id>/<video_id>/output.m3u8')
@token_required
def serve_hls_playlist(user_id, video_id):
    hls_dir = os.path.join(HLS_FOLDER, user_id, video_id)
    playlist_path = os.path.join(hls_dir, 'output.m3u8')
    
    if not os.path.exists(playlist_path):
        return jsonify({'message': 'Conversion in progress or failed'}), 404
    
    # Add Cache-Control headers
    response = send_file(playlist_path, mimetype='application/x-mpegURL')
    response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    
    return response


# Routes dédiées au mobile
@app.route('/mobile/hls/<user_id>/<video_id>/output.m3u8')
@token_required_mobile
def serve_mobile_hls_playlist(user_id, video_id):
    hls_dir = os.path.join(HLS_FOLDER, user_id, video_id)
    playlist_path = os.path.join(hls_dir, 'output.m3u8')
    if not os.path.exists(playlist_path):
        return jsonify({'message': 'Conversion in progress or failed'}), 404
    try:
        with open(playlist_path, 'r') as f:
            content = f.read()
        token = _get_token_from_request()
        base = f"https://server.focustagency.com/mobile/hls/{user_id}/{video_id}"
        lines = []
        for line in content.splitlines():
            if line.startswith('#EXT-X-KEY'):
                line = re.sub(r'URI=\"[^\"]+\"', f'URI="{base}/key?token={token}"', line)
                lines.append(line)
            elif line.strip() and not line.startswith('#') and line.strip().endswith('.ts'):
                seg = line.strip()
                abs_url = f"{base}/{seg}?token={token}"
                lines.append(abs_url)
            else:
                lines.append(line)
        new_content = "\n".join(lines)
        response = app.response_class(new_content, mimetype='application/x-mpegURL')
        response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
        response.headers['Pragma'] = 'no-cache'
        response.headers['Expires'] = '0'
        return response
    except Exception as e:
        app.logger.error(f"Error rewriting mobile playlist: {e}")
        return jsonify({'message': 'Internal server error'}), 500

@app.route('/mobile/hls/<user_id>/<video_id>/<segment>')
@token_required_mobile
def serve_mobile_hls_segment(user_id, video_id, segment):
    hls_dir = os.path.join(HLS_FOLDER, user_id, video_id)
    segment_path = os.path.join(hls_dir, segment)
    if not os.path.exists(segment_path):
        return jsonify({'message': 'Segment not found'}), 404
    response = send_file(segment_path)
    response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    return response

@app.route('/mobile/hls/<user_id>/<video_id>/key')
@token_required_mobile
def serve_mobile_hls_key(user_id, video_id):
    hls_dir = os.path.join(HLS_FOLDER, user_id, video_id)
    key_path = os.path.join(hls_dir, 'enc.key')
    if not os.path.exists(key_path):
        return jsonify({'message': 'Encryption key not found'}), 404
    response = send_file(key_path)
    response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    return response

@app.route('/hls/<user_id>/<video_id>/<segment>')
@token_required
def serve_hls_segment(user_id, video_id, segment):
    hls_dir = os.path.join(HLS_FOLDER, user_id, video_id)
    segment_path = os.path.join(hls_dir, segment)
    
    if not os.path.exists(segment_path):
        return jsonify({'message': 'Segment not found'}), 404
    
    # Add Cache-Control headers
    response = send_file(segment_path)
    response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    
    return response

@app.route('/hls/<user_id>/<video_id>/key')
@token_required
def serve_hls_key(user_id, video_id):
    hls_dir = os.path.join(HLS_FOLDER, user_id, video_id)
    key_path = os.path.join(hls_dir, 'enc.key')
    
    if not os.path.exists(key_path):
        return jsonify({'message': 'Encryption key not found'}), 404
    
    # Add Cache-Control headers
    response = send_file(key_path)
    response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    
    return response


@app.route('/upload_presentation', methods=['POST'])
def upload_presentation_video():
    if 'video' not in request.files:
        return jsonify({'message': 'Aucune vidéo uploadée'}), 400
    file = request.files['video']
    app.logger.info('Fichier reçu: %s, type: %s', file.filename, file.content_type)
    if file.filename == '':
        return jsonify({'message': 'Aucun fichier sélectionné'}), 400
    if file and allowed_file(file.filename):
        filename = secure_filename(file.filename)
        file_path = os.path.join(PRESENTATION_VIDEOS_FOLDER, filename)
        file.save(file_path)
        return jsonify({'message': 'Vidéo de présentation uploadée avec succès', 'file_path': file_path}), 201
    else:
        return jsonify({'message': 'Type de fichier non autorisé'}), 400

@app.route('/presentation_videos/<filename>')
def serve_presentation_video(filename):
    file_path = os.path.join(PRESENTATION_VIDEOS_FOLDER, filename)
    if not os.path.exists(file_path):
        abort(404)
    return send_file(file_path, conditional=True)

if __name__ == '__main__':
    os.makedirs(UPLOAD_FOLDER, exist_ok=True)
    os.makedirs(HLS_FOLDER, exist_ok=True)
    os.makedirs(PRESENTATION_VIDEOS_FOLDER, exist_ok=True)
    os.makedirs(ORIGINALS_FOLDER, exist_ok=True)
    app.run(debug=True, host='0.0.0.0', port=5000)