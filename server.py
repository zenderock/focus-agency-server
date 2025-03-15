from flask import Flask, render_template, request, redirect, url_for, send_from_directory, abort, send_file, jsonify
from flask_cors import CORS
import os
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
ALLOWED_EXTENSIONS = {'mp4', 'avi', 'mov', 'wmv', 'flv'}
SECRET_KEY = 'ntA4{Q6NLb?fRgs|]U^MV.u@d,m44IF(AFLm]-4=P-[gC5<u8_PvwYt-*.+Rgop_[www.zenderock.me]'
TOKEN_EXPIRY = 3600
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['MAX_CONTENT_LENGTH'] = 100 * 1024 * 1024
app.config['SECRET_KEY'] = SECRET_KEY

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
        token = request.args.get('token')
        
        if not token:
            return jsonify({'message': 'Token is missing'}), 403
            
        try:
            # Verify the token
            data = jwt.decode(token, app.config['SECRET_KEY'], algorithms=['HS256'])
            
            # Check if token is for the correct user and video
            if 'user_id' in kwargs and data.get('user_id') != kwargs['user_id']:
                raise Exception("Invalid user")
                
            if 'filename' in kwargs and data.get('filename') != kwargs['filename']:
                raise Exception("Invalid file")
                
            # Check referrer
            if request.referrer:
                allowed_referrers = [
                    r'https://focustagency\.com',
                    r'https://trainer\.focustagency\.com',
                    r'https://learner\.focustagency\.com'
                ]
                
                referrer_allowed = False
                for pattern in allowed_referrers:
                    if re.match(pattern, request.referrer):
                        referrer_allowed = True
                        break
                
                if not referrer_allowed:
                    raise Exception("Invalid referrer")
            else:
                # Optional: Block requests with no referrer
                raise Exception("No referrer")
                
        except Exception as e:
            print(f"Token verification failed: {str(e)}")
            return jsonify({'message': 'Token is invalid or expired'}), 403
            
        return f(*args, **kwargs)
    return decorated

@celery.task
def convert_to_hls(input_path: str, hls_dir: str, encryption_key=None):
    output_path = os.path.join(hls_dir, 'output.m3u8')
    key_path = os.path.join(hls_dir, 'enc.key')
    key_info_path = os.path.join(hls_dir, 'enc.keyinfo')
    
    if encryption_key is None:
        encryption_key = os.urandom(16)
    with open(key_path, 'wb') as key_file:
        key_file.write(encryption_key)
    
    # URL absolue pour la clé
    user_id = hls_dir.split(os.sep)[-2]  # Extrait user_id de hls_dir
    video_id = hls_dir.split(os.sep)[-1]  # Extrait video_id de hls_dir
    key_url = f"https://server.focustagency.com/hls/{user_id}/{video_id}/enc.key"
    
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
    except subprocess.CalledProcessError as e:
        app.logger.error(f"Erreur FFmpeg: {e}")
def allowed_file(filename):
    return '.' in filename and \
           filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

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

        # Définir le dossier HLS
        video_id = filename.split('.')[0]  # Utiliser le nom du fichier sans extension comme video_id
        hls_dir = os.path.join(HLS_FOLDER, user_id, video_id)
        os.makedirs(hls_dir, exist_ok=True)
        
        # Lancer la tâche asynchrone
        task = convert_to_hls.delay(file_path, hls_dir)

        # Renvoyer une réponse JSON
        return jsonify({
            'message': 'Conversion en cours',
            'task_id': task.id,
            'hls_path': f"/hls/{user_id}/{video_id}/output.m3u8"
        }), 202


# Generate a short-lived token for video access
def generate_video_token(user_id, filename, duration=TOKEN_EXPIRY):
    payload = {
        'user_id': user_id,
        'filename': filename,
        'exp': datetime.utcnow() + timedelta(seconds=duration),
        'iat': datetime.utcnow(),
        'jti': str(uuid.uuid4())
    }
    return jwt.encode(payload, app.config['SECRET_KEY'], algorithm='HS256')

@app.route('/api/get-video-token/<user_id>/<filename>')
def get_video_token(user_id, filename):
    # Supprimez tout le code relatif aux headers CORS manuels
    token = generate_video_token(user_id, filename)
    return jsonify({
        'token': token,
        'expires_in': TOKEN_EXPIRY
    })

@app.route('/api/get-video-token/<user_id>/<filename>', methods=['OPTIONS'])
def preflight(user_id, filename):
    return jsonify({'message': 'Preflight OK'}), 200


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

if __name__ == '__main__':
    os.makedirs(UPLOAD_FOLDER, exist_ok=True)
    os.makedirs(HLS_FOLDER, exist_ok=True)
    app.run(debug=True, host='0.0.0.0', port=5000)