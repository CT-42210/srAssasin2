import os
import uuid
from datetime import datetime
from flask import Blueprint, render_template, redirect, url_for, flash, request, current_app, send_from_directory
from flask_login import login_required, current_user
from werkzeug.utils import secure_filename

from app.models import db, Team, Player, KillConfirmation, GameState
from app.services.game_service import submit_kill as service_submit_kill
from app.services.email_service import send_kill_submission_notification, send_team_elimination_notification

game = Blueprint('game', __name__)


def allowed_file(filename):
    """Check if the file has an allowed extension."""
    return '.' in filename and \
        filename.rsplit('.', 1)[1].lower() in current_app.config['ALLOWED_EXTENSIONS']


@game.route('/home')
@login_required
def home():
    """
    Home page for logged-in players.
    """
    # Get game state
    game_state = GameState.query.first()

    # Get current player's team
    team = Team.query.get(current_user.team_id)

    # Get teammate if exists
    teammate = Player.query.filter_by(team_id=team.id).filter(Player.id != current_user.id).first()

    # Get target team and players if assigned
    target_team = None
    target_players = []
    if team.target_id:
        target_team = Team.query.get(team.target_id)
        if target_team:
            target_players = Player.query.filter_by(team_id=target_team.id).all()

    # Get counts of teams and players still alive
    alive_teams_count = Team.query.filter_by(state='alive').count()
    alive_players_count = Player.query.filter_by(state='alive').count()

    return render_template(
        'game/home.html',
        game_state=game_state,
        team=team,
        teammate=teammate,
        target_team=target_team,
        target_players=target_players,
        alive_teams=alive_teams_count,
        alive_players=alive_players_count,
        round_end=game_state.round_end,
        now=datetime.now()
    )


@game.route('/submit-kill', methods=['GET', 'POST'])
@login_required
def submit_kill_route():
    """
    Handle kill submission form.
    """
    # Get game state
    game_state = GameState.query.first()

    # Only allow kill submissions during live game
    if game_state.state != 'live':
        flash('The game is not currently active.', 'danger')
        return redirect(url_for('game.home'))

    # Only alive players can submit kills
    if not current_user.is_alive:
        flash('You cannot submit kills because you are eliminated.', 'danger')
        return redirect(url_for('game.home'))

    # Only players from alive teams can submit kills
    team = Team.query.get(current_user.team_id)
    if not team.is_alive:
        flash('Your team is eliminated and cannot submit kills.', 'danger')
        return redirect(url_for('game.home'))

    # Get target team and players if assigned
    target_team = None
    target_players = []
    if team.target_id:
        target_team = Team.query.get(team.target_id)
        if target_team:
            target_players = [p for p in Player.query.filter_by(team_id=target_team.id).all() if p.is_alive]

    # If no targets, redirect back to home
    if not target_team or not target_players:
        flash('You have no valid targets.', 'danger')
        return redirect(url_for('game.home'))

    if request.method == 'POST':
        victim_id = request.form.get('victim_id')
        kill_time_str = request.form.get('kill_time')
        rules_confirmation = 'rules_confirmed' in request.form

        # Validate inputs
        if not victim_id or not kill_time_str or not rules_confirmation:
            flash('All fields are required.', 'danger')
            return render_template('game/submit_kill.html', target_team=target_team, target_players=target_players,
                                   now=datetime.now())

        # Parse kill time
        try:
            # Expecting format like "2023-09-15T15:30"
            kill_time = datetime.strptime(kill_time_str, '%Y-%m-%dT%H:%M')
        except ValueError:
            flash('Invalid time format.', 'danger')
            return render_template('game/submit_kill.html', target_team=target_team, target_players=target_players,
                                   now=datetime.now())

        # Check if a video was uploaded
        if 'kill_video' not in request.files:
            flash('No video uploaded.', 'danger')
            return render_template('game/submit_kill.html', target_team=target_team, target_players=target_players,
                                   now=datetime.now())

        file = request.files['kill_video']

        # Check if file is empty
        if file.filename == '':
            flash('No video selected.', 'danger')
            return render_template('game/submit_kill.html', target_team=target_team, target_players=target_players,
                                   now=datetime.now())

        # Check if file has allowed extension
        if not allowed_file(file.filename):
            flash('Invalid file type. Allowed types: mp4, mov.', 'danger')
            return render_template('game/submit_kill.html', target_team=target_team, target_players=target_players,
                                   now=datetime.now())

        # Save the file
        filename = secure_filename(file.filename)
        file_ext = filename.rsplit('.', 1)[1].lower()
        unique_filename = f"/kill_{uuid.uuid4().hex}.{file_ext}"
        print(current_app.config['UPLOAD_FOLDER'] + unique_filename)
        file_path = current_app.config['UPLOAD_FOLDER'] + unique_filename
        print(file_path)
        file.save(file_path)

        # The relative path that will be stored in the database
        relative_path = f"uploads/{unique_filename}"

        # Then update the submit_kill call:
        kill_confirmation = service_submit_kill(
            victim_id=victim_id,
            attacker_id=current_user.id,
            kill_time=kill_time,
            video_path=relative_path  # Pass the relative path
        )

        if kill_confirmation:
            # Send notification to all players to vote on this kill
            send_kill_submission_notification(kill_confirmation)

            # Check if the victim's team is now eliminated
            victim = Player.query.get(victim_id)
            victim_team = Team.query.get(victim.team_id)
            if victim_team.all_dead:
                # Update team state to "eliminated" if not already
                if victim_team.state != 'eliminated':
                    victim_team.state = 'eliminated'
                    victim_team.eliminated_in_round = game_state.current_round
                    db.session.commit()

                    # Send elimination notification to the team
                    send_team_elimination_notification(victim_team.id)

            flash('Kill submitted successfully and pending confirmation.', 'success')
            return redirect(url_for('game.home'))
        else:
            flash('Failed to submit kill. Please check your inputs and try again.', 'danger')
            return redirect(url_for('game.submit_kill_route'))

    return render_template('game/submit_kill.html', target_team=target_team, target_players=target_players,
                           now=datetime.now())
