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

DEFAULT_ALLOWED_ORIGINS = [
    "https://focustagency.com",
    "https://trainer.focustagency.com",
    "https://learner.focustagency.com",
    "http://localhost:3000"
]

app = Flask(__name__)

# Configuration
UPLOAD_FOLDER = 'uploads'
HLS_FOLDER = 'hls'
PRESENTATION_VIDEOS_FOLDER = 'presentation_videos'
ORIGINALS_FOLDER = 'originals'
COURSE_PRESENTATION_SUBFOLDER = 'courses'
MODULE_PRESENTATION_SUBFOLDER = 'modules'

ALLOWED_EXTENSIONS = {'mp4', 'avi', 'mov', 'wmv', 'flv'}

def _load_env_from_file(path: str = ".env"):
    try:
        if os.path.exists(path):
            with open(path, 'r') as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith('#'):
                        continue
                    if '=' in line:
                        k, v = line.split('=', 1)
                        k = k.strip()
                        v = v.strip().strip('"').strip("'")
                        if k and (k not in os.environ):
                            os.environ[k] = v
    except Exception:
        pass

_load_env_from_file()

SECRET_KEY = os.getenv('SECRET_KEY', 'CHANGE_ME_DEV_SECRET')
TOKEN_EXPIRY = int(os.getenv('TOKEN_EXPIRY', '3600'))
CALLBACK_BEARER = os.getenv('CALLBACK_BEARER', '')
DOWNLOAD_TOKEN_REQUIRE_FILENAME = os.getenv('DOWNLOAD_TOKEN_REQUIRE_FILENAME', 'false').lower() == 'true'

app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['MAX_CONTENT_LENGTH'] = 100 * 1024 * 1024
app.config['SECRET_KEY'] = SECRET_KEY
app.config['MESOMB_APP_KEY'] = os.getenv('MESOMB_APP_KEY', '')
app.config['MESOMB_API_KEY'] = os.getenv('MESOMB_API_KEY', '')
app.config['MESOMB_API_SECRET'] = os.getenv('MESOMB_API_SECRET', '')
app.config['DOWNLOAD_TOKEN_REQUIRE_FILENAME'] = DOWNLOAD_TOKEN_REQUIRE_FILENAME

# CORS origins from env or defaults
_env_origins = os.getenv('FOCUST_ALLOWED_ORIGINS', '').strip()
if _env_origins:
    FOCUST_ALLOWED_ORIGINS = [o.strip() for o in _env_origins.split(',') if o.strip()]
else:
    FOCUST_ALLOWED_ORIGINS = DEFAULT_ALLOWED_ORIGINS

# Initialize CORS after env is loaded
CORS(app,
     resources={r"/*": {"origins": FOCUST_ALLOWED_ORIGINS}},
     allow_headers=["Content-Type", "Authorization"],
     methods=["GET", "POST", "OPTIONS"]
)

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
                
            # Binding optionnel au chemin HLS v2
            if 'rel' in kwargs and data.get('rel') and data.get('rel') != kwargs['rel']:
                raise Exception('Invalid rel')
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
            # Binding optionnel au chemin HLS v2
            if 'rel' in kwargs and data.get('rel') and data.get('rel') != kwargs['rel']:
                raise Exception('Invalid rel')
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
            # Vérification optionnelle du filename selon flag DOWNLOAD_TOKEN_REQUIRE_FILENAME
            if app.config.get('DOWNLOAD_TOKEN_REQUIRE_FILENAME', True):
                if 'filename' in kwargs and data.get('filename') != kwargs['filename']:
                    raise Exception('Invalid file')
            # Vérification hiérarchique v2 si présente
            if 'rel' in kwargs and data.get('rel') and data.get('rel') != kwargs['rel']:
                raise Exception('Invalid rel')
            if 'dtype' in kwargs and data.get('type') and data.get('type') != kwargs['dtype']:
                raise Exception('Invalid type')
            if 'course_id' in kwargs and data.get('course_id') and data.get('course_id') != kwargs['course_id']:
                raise Exception('Invalid course_id')
            if 'module_id' in kwargs and data.get('module_id') and data.get('module_id') != kwargs['module_id']:
                raise Exception('Invalid module_id')
        except Exception as e:
            print(f"Download token verification failed: {str(e)}")
            return jsonify({'message': 'Token is invalid or expired'}), 403
        return f(*args, **kwargs)
    return decorated

@celery.task
def convert_to_hls(input_path: str, hls_dir: str, success_callback_url=None, error_callback_url=None, user_id=None, video_id=None, encryption_key=None, context=None, key_url=None):
    output_path = os.path.join(hls_dir, 'output.m3u8')
    key_path = os.path.join(hls_dir, 'enc.key')
    key_info_path = os.path.join(hls_dir, 'enc.keyinfo')
    
    if encryption_key is None:
        encryption_key = os.urandom(16)
    with open(key_path, 'wb') as key_file:
        key_file.write(encryption_key)
    
    # URL absolue pour la clé
    if key_url is None:
        try:
            rel = os.path.relpath(hls_dir, HLS_FOLDER)
            key_url = f"https://server.focustagency.com/hls2/{rel}/key"
        except Exception:
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
                if context:
                    payload['context'] = context
                    try:
                        rel = os.path.relpath(hls_dir, HLS_FOLDER)
                        payload['hls_path'] = f"/hls2/{rel}/output.m3u8"
                    except Exception:
                        pass
                headers = {"Content-Type": "application/json"}
                if CALLBACK_BEARER:
                    headers["Authorization"] = f"Bearer {CALLBACK_BEARER}"
                requests.post(success_callback_url, json=payload, timeout=10, headers=headers)
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
                if context:
                    payload['context'] = context
                headers = {"Content-Type": "application/json"}
                if CALLBACK_BEARER:
                    headers["Authorization"] = f"Bearer {CALLBACK_BEARER}"
                requests.post(error_callback_url, json=payload, timeout=10, headers=headers)
            except Exception as callback_error:
                app.logger.error(f"Erreur callback échec: {callback_error}")
        
        raise e
def allowed_file(filename):
    return '.' in filename and \
           filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

# Helpers hiérarchiques
def _lesson_paths(trainer_id: str, course_id: str, module_id: str, lesson_id: str, filename: str):
    safe_name = secure_filename(filename)
    uploads_path = os.path.join(UPLOAD_FOLDER, trainer_id, course_id, module_id, lesson_id)
    originals_path = os.path.join(ORIGINALS_FOLDER, trainer_id, course_id, module_id, lesson_id)
    hls_dir = os.path.join(HLS_FOLDER, trainer_id, course_id, module_id, lesson_id)
    return uploads_path, originals_path, hls_dir, safe_name

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


# Upload d'une leçon (chiffré HLS) avec hiérarchie Formation > Module > Leçon
@app.route('/upload/lesson', methods=['POST'])
def upload_lesson_video():
    if 'video' not in request.files:
        return jsonify({'message': 'Aucune vidéo uploadée'}), 400
    file = request.files['video']
    app.logger.info('Fichier reçu: %s, type: %s', file.filename, file.content_type)
    if file.filename == '':
        return jsonify({'message': 'Aucun fichier sélectionné'}), 400
    trainer_id = request.form.get('trainer_id')
    course_id = request.form.get('course_id')
    module_id = request.form.get('module_id')
    lesson_id = request.form.get('lesson_id')
    success_callback_url = request.form.get('success_callback_url') or "https://pjtwnneriajuppezuqqs.supabase.co/functions/v1/update-lesson-conversion-status"
    error_callback_url = request.form.get('error_callback_url') or "https://pjtwnneriajuppezuqqs.supabase.co/functions/v1/update-lesson-conversion-status"
    if not all([trainer_id, course_id, module_id, lesson_id]):
        return jsonify({'message': 'Champs requis manquants (trainer_id, course_id, module_id, lesson_id)'}), 400
    if not allowed_file(file.filename):
        return jsonify({'message': 'Type de fichier non autorisé'}), 400
    # Appliquer la nomenclature: <lesson_id>_lesson<extension>
    orig_name = secure_filename(file.filename)
    _, ext = os.path.splitext(orig_name)
    ext = ext.lower()
    target_filename = f"{lesson_id}_lesson{ext}"
    uploads_path, originals_path, hls_dir, safe_name = _lesson_paths(trainer_id, course_id, module_id, lesson_id, target_filename)
    os.makedirs(uploads_path, exist_ok=True)
    os.makedirs(originals_path, exist_ok=True)
    os.makedirs(hls_dir, exist_ok=True)
    temp_path = os.path.join(uploads_path, safe_name)
    file.save(temp_path)
    try:
        shutil.copy2(temp_path, os.path.join(originals_path, safe_name))
    except Exception as e:
        app.logger.error(f"Échec copie original: {e}")
    context = {
        'trainer_id': trainer_id,
        'course_id': course_id,
        'module_id': module_id,
        'lesson_id': lesson_id
    }
    try:
        rel = os.path.relpath(hls_dir, HLS_FOLDER)
        hls_path_public = f"/hls2/{rel}/output.m3u8"
    except Exception:
        hls_path_public = None
    task = convert_to_hls.delay(
        temp_path,
        hls_dir,
        success_callback_url,
        error_callback_url,
        None,
        lesson_id,
        None,
        context,
        None
    )
    return jsonify({
        'message': 'Conversion en cours',
        'task_id': task.id,
        'lesson_id': lesson_id,
        'hls_path': hls_path_public
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

# Generate a short-lived token for hierarchical download (v2)
def generate_download_token_v2(user_id: str, dtype: str, filename: str | None, duration: int = 900, rel: str | None = None, course_id: str | None = None, module_id: str | None = None):
    payload = {
        'sub': 'download',
        'user_id': user_id,
        'type': dtype,
        'exp': datetime.utcnow() + timedelta(seconds=duration),
        'iat': datetime.utcnow(),
        'jti': str(uuid.uuid4()),
        'platform': 'download'
    }
    if filename:
        payload['filename'] = filename
    if dtype == 'lesson' and rel:
        payload['rel'] = rel
    if dtype in ('course', 'module'):
        if course_id:
            payload['course_id'] = course_id
        if module_id and dtype == 'module':
            payload['module_id'] = module_id
    return jwt.encode(payload, app.config['SECRET_KEY'], algorithm='HS256')

# Generate a short-lived token for hierarchical HLS v2 access
def generate_video_token_v2(user_id, rel, duration=TOKEN_EXPIRY, platform='web'):
    payload = {
        'user_id': user_id,
        'rel': rel,
        'exp': datetime.utcnow() + timedelta(seconds=duration),
        'iat': datetime.utcnow(),
        'jti': str(uuid.uuid4()),
        'platform': platform
    }
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


# Endpoint to issue v2 token tied to hierarchical rel path
@app.route('/api/get-video-token/v2')
def get_video_token_v2():
    try:
        ttl = int(request.args.get('ttl', TOKEN_EXPIRY))
    except Exception:
        ttl = TOKEN_EXPIRY
    user_id = request.args.get('user_id')
    rel = request.args.get('rel')
    platform = request.args.get('platform', 'web')
    if not user_id or not rel:
        return jsonify({'message': 'user_id et rel sont requis'}), 400
    if platform not in ('web', 'mobile'):
        return jsonify({'message': 'platform invalide'}), 400
    token = generate_video_token_v2(user_id, rel, duration=ttl, platform=platform)
    if platform == 'mobile':
        playlist_url = f"https://server.focustagency.com/mobile/hls2/{rel}/output.m3u8"
    else:
        playlist_url = f"https://server.focustagency.com/hls2/{rel}/output.m3u8"
    return jsonify({
        'token': token,
        'expires_in': ttl,
        'playlist_url': playlist_url
    })

# Endpoint pour émettre un token de téléchargement v2 (hiérarchique)
@app.route('/api/get-download-token/v2')
def get_download_token_v2():
    try:
        ttl = int(request.args.get('ttl', 900))
    except Exception:
        ttl = 900
    user_id = request.args.get('user_id')
    dtype = request.args.get('type')  # 'lesson' | 'course' | 'module'
    filename = request.args.get('filename')
    rel = request.args.get('rel')
    course_id = request.args.get('course_id')
    module_id = request.args.get('module_id')
    require_filename = app.config.get('DOWNLOAD_TOKEN_REQUIRE_FILENAME', True)
    if not user_id or not dtype:
        return jsonify({'message': 'user_id et type requis'}), 400
    if require_filename and not filename:
        return jsonify({'message': 'filename requis (flag DOWNLOAD_TOKEN_REQUIRE_FILENAME=true)'}), 400
    if dtype not in ('lesson', 'course', 'module'):
        return jsonify({'message': 'type invalide'}), 400
    if dtype == 'lesson' and not rel:
        return jsonify({'message': 'rel requis pour type=lesson'}), 400
    if dtype == 'course' and not course_id:
        return jsonify({'message': 'course_id requis pour type=course'}), 400
    if dtype == 'module' and (not course_id or not module_id):
        return jsonify({'message': 'course_id et module_id requis pour type=module'}), 400

    token = generate_download_token_v2(user_id, dtype, filename, duration=ttl, rel=rel, course_id=course_id, module_id=module_id)

    response = {'token': token, 'expires_in': ttl}
    if dtype == 'lesson':
        if filename:
            response['download_url'] = f"https://server.focustagency.com/download2/{rel}/{filename}"
        else:
            response['download_base_url'] = f"https://server.focustagency.com/download2/{rel}"
            try:
                folder = os.path.join(ORIGINALS_FOLDER, rel)
                if os.path.isdir(folder):
                    files = [f for f in os.listdir(folder) if os.path.isfile(os.path.join(folder, f))]
                    if len(files) == 1:
                        _, ext = os.path.splitext(files[0])
                        response['extension'] = ext.lower()
            except Exception as _e:
                pass
    elif dtype == 'course':
        if filename:
            response['download_url'] = f"https://server.focustagency.com/download2/course/{course_id}/{filename}"
        else:
            response['download_base_url'] = f"https://server.focustagency.com/download2/course/{course_id}"
    else:
        if filename:
            response['download_url'] = f"https://server.focustagency.com/download2/module/{course_id}/{module_id}/{filename}"
        else:
            response['download_base_url'] = f"https://server.focustagency.com/download2/module/{course_id}/{module_id}"

    return jsonify(response)

@app.route('/api/get-video-token/v2', methods=['OPTIONS'])
def preflight_get_video_token_v2():
    return jsonify({'message': 'Preflight OK'}), 200

# Preflight pour download-token v2
@app.route('/api/get-download-token/v2', methods=['OPTIONS'])
def preflight_get_download_token_v2():
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

# Téléchargement hiérarchique v2 — Leçon (original)
@app.route('/download2/<path:rel>/<filename>')
@token_required_download
def download2_lesson(rel, filename):
    base = os.path.join(ORIGINALS_FOLDER, rel)
    file_path = os.path.join(base, filename)
    if not os.path.isfile(file_path):
        return jsonify({'message': 'File not found'}), 404
    try:
        response = send_file(file_path, as_attachment=True)
        response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
        response.headers['Pragma'] = 'no-cache'
        response.headers['Expires'] = '0'
        return response
    except Exception as e:
        app.logger.error(f"Download2 lesson failed: {e}")
        return jsonify({'message': 'Internal server error'}), 500

# Téléchargement hiérarchique v2 — Présentation Formation
@app.route('/download2/course/<course_id>/<filename>')
@token_required_download
def download2_course(course_id, filename):
    folder = os.path.join(PRESENTATION_VIDEOS_FOLDER, COURSE_PRESENTATION_SUBFOLDER, secure_filename(course_id))
    file_path = os.path.join(folder, filename)
    if not os.path.isfile(file_path):
        return jsonify({'message': 'File not found'}), 404
    try:
        response = send_file(file_path, as_attachment=True)
        response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
        response.headers['Pragma'] = 'no-cache'
        response.headers['Expires'] = '0'
        return response
    except Exception as e:
        app.logger.error(f"Download2 course failed: {e}")
        return jsonify({'message': 'Internal server error'}), 500

# Téléchargement hiérarchique v2 — Présentation Module
@app.route('/download2/module/<course_id>/<module_id>/<filename>')
@token_required_download
def download2_module(course_id, module_id, filename):
    folder = os.path.join(PRESENTATION_VIDEOS_FOLDER, MODULE_PRESENTATION_SUBFOLDER, secure_filename(course_id), secure_filename(module_id))
    file_path = os.path.join(folder, filename)
    if not os.path.isfile(file_path):
        return jsonify({'message': 'File not found'}), 404
    try:
        response = send_file(file_path, as_attachment=True)
        response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
        response.headers['Pragma'] = 'no-cache'
        response.headers['Expires'] = '0'
        return response
    except Exception as e:
        app.logger.error(f"Download2 module failed: {e}")
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

# Upload vidéo de présentation Formation (non chiffrée)
@app.route('/upload_presentation/course/<course_id>', methods=['POST'])
def upload_presentation_course(course_id):
    if 'video' not in request.files:
        return jsonify({'message': 'Aucune vidéo uploadée'}), 400
    file = request.files['video']
    if file.filename == '':
        return jsonify({'message': 'Aucun fichier sélectionné'}), 400
    if not allowed_file(file.filename):
        return jsonify({'message': 'Type de fichier non autorisé'}), 400
    folder = os.path.join(PRESENTATION_VIDEOS_FOLDER, COURSE_PRESENTATION_SUBFOLDER, secure_filename(course_id))
    os.makedirs(folder, exist_ok=True)
    ext = file.filename.rsplit('.', 1)[1].lower()
    path = os.path.join(folder, f'presentation.{ext}')
    file.save(path)
    return jsonify({'message': 'OK', 'file_path': path}), 201

# Upload vidéo de présentation Module (non chiffrée)
@app.route('/upload_presentation/module/<course_id>/<module_id>', methods=['POST'])
def upload_presentation_module(course_id, module_id):
    if 'video' not in request.files:
        return jsonify({'message': 'Aucune vidéo uploadée'}), 400
    file = request.files['video']
    if file.filename == '':
        return jsonify({'message': 'Aucun fichier sélectionné'}), 400
    if not allowed_file(file.filename):
        return jsonify({'message': 'Type de fichier non autorisé'}), 400
    folder = os.path.join(PRESENTATION_VIDEOS_FOLDER, MODULE_PRESENTATION_SUBFOLDER, secure_filename(course_id), secure_filename(module_id))
    os.makedirs(folder, exist_ok=True)
    ext = file.filename.rsplit('.', 1)[1].lower()
    path = os.path.join(folder, f'presentation.{ext}')
    file.save(path)
    return jsonify({'message': 'OK', 'file_path': path}), 201

# Serve vidéo de présentation Formation
@app.route('/presentation_videos/course/<course_id>')
def serve_presentation_course(course_id):
    folder = os.path.join(PRESENTATION_VIDEOS_FOLDER, COURSE_PRESENTATION_SUBFOLDER, secure_filename(course_id))
    if not os.path.isdir(folder):
        abort(404)
    # chercher un fichier presentation.*
    for name in os.listdir(folder):
        if name.startswith('presentation.'):
            return send_file(os.path.join(folder, name), conditional=True)
    abort(404)

# Serve vidéo de présentation Module
@app.route('/presentation_videos/module/<course_id>/<module_id>')
def serve_presentation_module(course_id, module_id):
    folder = os.path.join(PRESENTATION_VIDEOS_FOLDER, MODULE_PRESENTATION_SUBFOLDER, secure_filename(course_id), secure_filename(module_id))
    if not os.path.isdir(folder):
        abort(404)
    for name in os.listdir(folder):
        if name.startswith('presentation.'):
            return send_file(os.path.join(folder, name), conditional=True)
    abort(404)

# HLS hiérarchique (web)
@app.route('/hls2/<path:rel>/output.m3u8')
@token_required
def serve_hls2_playlist(rel):
    hls_dir = os.path.join(HLS_FOLDER, rel)
    playlist_path = os.path.join(hls_dir, 'output.m3u8')
    if not os.path.exists(playlist_path):
        return jsonify({'message': 'Conversion in progress or failed'}), 404
    response = send_file(playlist_path, mimetype='application/x-mpegURL')
    response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    return response

@app.route('/hls2/<path:rel>/<segment>')
@token_required
def serve_hls2_segment(rel, segment):
    hls_dir = os.path.join(HLS_FOLDER, rel)
    segment_path = os.path.join(hls_dir, segment)
    if not os.path.exists(segment_path):
        return jsonify({'message': 'Segment not found'}), 404
    response = send_file(segment_path)
    response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    return response

@app.route('/hls2/<path:rel>/key')
@token_required
def serve_hls2_key(rel):
    hls_dir = os.path.join(HLS_FOLDER, rel)
    key_path = os.path.join(hls_dir, 'enc.key')
    if not os.path.exists(key_path):
        return jsonify({'message': 'Encryption key not found'}), 404
    response = send_file(key_path)
    response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    return response

# HLS hiérarchique (mobile)
@app.route('/mobile/hls2/<path:rel>/output.m3u8')
@token_required_mobile
def serve_mobile_hls2_playlist(rel):
    hls_dir = os.path.join(HLS_FOLDER, rel)
    playlist_path = os.path.join(hls_dir, 'output.m3u8')
    if not os.path.exists(playlist_path):
        return jsonify({'message': 'Conversion in progress or failed'}), 404
    try:
        with open(playlist_path, 'r') as f:
            content = f.read()
        token = _get_token_from_request()
        base = f"https://server.focustagency.com/mobile/hls2/{rel}"
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

@app.route('/mobile/hls2/<path:rel>/<segment>')
@token_required_mobile
def serve_mobile_hls2_segment(rel, segment):
    hls_dir = os.path.join(HLS_FOLDER, rel)
    segment_path = os.path.join(hls_dir, segment)
    if not os.path.exists(segment_path):
        return jsonify({'message': 'Segment not found'}), 404
    response = send_file(segment_path)
    response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    return response

@app.route('/mobile/hls2/<path:rel>/key')
@token_required_mobile
def serve_mobile_hls2_key(rel):
    hls_dir = os.path.join(HLS_FOLDER, rel)
    key_path = os.path.join(hls_dir, 'enc.key')
    if not os.path.exists(key_path):
        return jsonify({'message': 'Encryption key not found'}), 404
    response = send_file(key_path)
    response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    return response

if __name__ == '__main__':
    os.makedirs(UPLOAD_FOLDER, exist_ok=True)
    os.makedirs(HLS_FOLDER, exist_ok=True)
    os.makedirs(PRESENTATION_VIDEOS_FOLDER, exist_ok=True)
    os.makedirs(ORIGINALS_FOLDER, exist_ok=True)
    os.makedirs(os.path.join(PRESENTATION_VIDEOS_FOLDER, COURSE_PRESENTATION_SUBFOLDER), exist_ok=True)
    os.makedirs(os.path.join(PRESENTATION_VIDEOS_FOLDER, MODULE_PRESENTATION_SUBFOLDER), exist_ok=True)
    app.run(debug=True, host='0.0.0.0', port=5000)