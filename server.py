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

app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": [
    "https://focustagency.com",
    "https://*.focustagency.com"
]}})

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
                if not re.match(r'https://focustagency\.com', request.referrer):
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
    
    # Generate encryption key if not provided
    if not encryption_key:
        encryption_key = os.urandom(16).hex()
        
    # Save the key to a file
    key_path = os.path.join(hls_dir, 'enc.key')
    with open(key_path, 'w') as f:
        f.write(encryption_key)
        
    # Create key info file
    key_info_path = os.path.join(hls_dir, 'enc.keyinfo')
    with open(key_info_path, 'w') as f:
        f.write(f'{key_path}\n')  # Path to the key file
        f.write(f'{key_path}\n')  # Path where to store the key on the server
        f.write(encryption_key)   # IV (same as the key in this example)
    
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
        # Optionally delete original file after conversion
        # os.remove(input_path)
        return {'status': 'success', 'key': encryption_key}
    except subprocess.CalledProcessError as e:
        print(f"Error during conversion: {e}")
        return {'status': 'error', 'message': str(e)}

def allowed_file(filename):
    return '.' in filename and \
           filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

@app.route('/', methods=['GET', 'POST'])
def index():
    if request.method == 'POST':
        if 'video' not in request.files:
            return 'No video file sent'
        file = request.files['video']
        app.logger.info('File received: %s, type: %s', file.filename, file.content_type)
        user_id = request.form.get('user_id') 
        if file.filename == '':
            return 'No file selected'
        if not user_id:
            return 'User ID missing'
        if file and allowed_file(file.filename):
            filename = secure_filename(file.filename)
            user_folder = os.path.join(app.config['UPLOAD_FOLDER'], user_id)
            os.makedirs(user_folder, exist_ok=True) 
            file_path = os.path.join(user_folder, filename) 
            file.save(file_path) 
            
            # Start HLS conversion with encryption
            hls_dir = os.path.join(HLS_FOLDER, user_id, filename.split('.')[0])
            os.makedirs(hls_dir, exist_ok=True)
            task = convert_to_hls.delay(file_path, hls_dir)
            
            return redirect(url_for('index'))
    return render_template('index.html', os=os)

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
    # Here you would normally authenticate the request
    # to ensure the user has permission to access this video
    
    # Generate a short-lived token
    token = generate_video_token(user_id, filename)
    
    return jsonify({
        'token': token,
        'expires_in': TOKEN_EXPIRY
    })

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