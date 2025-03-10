from flask import Flask, render_template, request, redirect, url_for, send_from_directory, abort
import os
from werkzeug.utils import secure_filename

app = Flask(__name__)

UPLOAD_FOLDER = 'uploads'
ALLOWED_EXTENSIONS = {'mp4', 'avi', 'mov', 'wmv', 'flv'}
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['MAX_CONTENT_LENGTH'] = 100 * 1024 * 1024

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
    if 'X-Focus' not in request.headers or request.headers['X-Focus'] != 'stream_allowed':
        abort(403)  

    user_folder = os.path.join(app.config['UPLOAD_FOLDER'], user_id) 
    return send_from_directory(user_folder, filename) 

if __name__ == '__main__':
    os.makedirs(UPLOAD_FOLDER, exist_ok=True)
    app.run(debug=True, host='0.0.0.0', port=5000)