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
        user_id = request.form.get('user_id') # Récupérer l'ID utilisateur depuis le formulaire

        if file.filename == '':
            return 'Aucun fichier sélectionné'
        if not user_id: # Vérification si l'ID utilisateur est fourni
            return 'ID Utilisateur manquant'

        if file and allowed_file(file.filename):
            filename = secure_filename(file.filename)

            # --- Structure par ID utilisateur ---
            user_folder = os.path.join(app.config['UPLOAD_FOLDER'], user_id) # Dossier utilisateur
            os.makedirs(user_folder, exist_ok=True) # Créer le dossier utilisateur s'il n'existe pas
            file_path = os.path.join(user_folder, filename) # Chemin complet du fichier

            file.save(file_path) # Sauvegarder dans le dossier utilisateur
            return redirect(url_for('index'))

    return render_template('index.html', os=os)


@app.route('/videos/<user_id>/<filename>') # Route modifiée pour inclure l'ID utilisateur
def serve_video(user_id, filename): # Fonction modifiée pour accepter l'ID utilisateur
    # Vérification du header x-focus
    if 'X-Focus' not in request.headers or request.headers['X-Focus'] != 'stream_allowed':
        abort(403)  # Retourne une erreur 403 si le header est manquant ou incorrect

    user_folder = os.path.join(app.config['UPLOAD_FOLDER'], user_id) # Reconstruire le chemin du dossier utilisateur
    return send_from_directory(user_folder, filename) # Servir la vidéo depuis le dossier utilisateur

if __name__ == '__main__':
    os.makedirs(UPLOAD_FOLDER, exist_ok=True)
    app.run(debug=True, host='0.0.0.0', port=5000)