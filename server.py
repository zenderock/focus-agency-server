from flask import Flask, render_template, request, redirect, url_for, send_from_directory, abort, send_file, jsonify
from flask_cors import CORS
import os
import subprocess
from werkzeug.utils import secure_filename
from celery import Celery

app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}})

UPLOAD_FOLDER = 'uploads'
HLS_FOLDER = '/hls'
ALLOWED_EXTENSIONS = {'mp4', 'avi', 'mov', 'wmv', 'flv'}
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['MAX_CONTENT_LENGTH'] = 100 * 1024 * 1024

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

@celery.task
def convert_to_hls(input_path: str, hls_dir: str):
    output_path = os.path.join(hls_dir, 'output.m3u8')
    cmd = [
        'ffmpeg',
        '-i', input_path,
        '-c:v', 'h264',
        '-c:a', 'aac',
        '-f', 'hls',
        '-hls_time', '10',
        '-hls_list_size', '0',
        output_path
    ]
    try:
        subprocess.run(cmd, check=True)
        # Optionnel : supprimer le fichier original après conversion
        # os.remove(input_path)
    except subprocess.CalledProcessError as e:
        print(f"Erreur lors de la conversion : {e}")
        raise

def allowed_file(filename):
    return '.' in filename and \
           filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

@app.route('/', methods=['GET', 'POST'])
def index():
    if request.method == 'POST':
        if 'video' not in request.files:
            return 'Aucun fichier vidéo envoyé'
        file = request.files['video']
        app.logger.info('Fichier reçu: %s, type: %s', file.filename, file.content_type)
        user_id = request.form.get('user_id') 
        if file.filename == '':
            return 'Aucun fichier sélectionné'
        if not user_id:
            return 'ID Utilisateur manquant'
        if file and allowed_file(file.filename):
            filename = secure_filename(file.filename)
            user_folder = os.path.join(app.config['UPLOAD_FOLDER'], user_id)
            os.makedirs(user_folder, exist_ok=True) 
            file_path = os.path.join(user_folder, filename) 
            file.save(file_path) 
            return redirect(url_for('index'))
    return render_template('index.html', os=os)

@app.route('/videos/<user_id>/<filename>')
def serve_video(user_id, filename):
    if request.args.get('token') != 'stream_allowed': 
        abort(403)
        
    user_folder = os.path.join(app.config['UPLOAD_FOLDER'], user_id)
    file_path = os.path.join(user_folder, filename)
    
    return send_file(file_path, conditional=True)


@app.route('/upload', methods=['POST'])
def upload_video():
    file = request.files.get('video')
    if not file:
        return jsonify({'message': 'Aucune vidéo uploadée'}), 400

    filename = file.filename
    input_path = os.path.join(UPLOAD_FOLDER, filename)
    file.save(input_path)

    # Créer un dossier pour les segments HLS
    hls_dir = os.path.join(HLS_FOLDER, filename.split('.')[0])
    os.makedirs(hls_dir, exist_ok=True)

    # Lancer la tâche asynchrone
    task = convert_to_hls.delay(input_path, hls_dir)

    return jsonify({
        'message': 'Conversion en cours',
        'task_id': task.id,
        'hls_path': f"/videos/{filename.split('.')[0]}/playlist.m3u8"
    }), 202

@app.route('/videos/<video_id>/playlist.m3u8')
def serve_hls_playlist(video_id):
    hls_dir = os.path.join(HLS_FOLDER, video_id)
    playlist_path = os.path.join(hls_dir, 'output.m3u8')
    if os.path.exists(playlist_path):
        return send_file(playlist_path, mimetype='application/x-mpegURL')
    return jsonify({'message': 'Conversion en cours ou échouée'}), 404

if __name__ == '__main__':
    os.makedirs(UPLOAD_FOLDER, exist_ok=True)
    os.makedirs(HLS_FOLDER, exist_ok=True)
    app.run(debug=True, host='0.0.0.0', port=5000)

