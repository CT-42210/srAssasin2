import datetime
import os
import shutil

from flask import current_app
from sqlalchemy import text
from werkzeug.security import check_password_hash

from app.models import db, Team, Player, GameState, KillConfirmation, KillVote, ActionLog
from app.services.admin_email_service import send_admin_image
from app.services.game_service import check_game_complete


def verify_admin_password(password):
    """
    Verify the admin password against the stored hash.

    Args:
        password (str): Password to verify

    Returns:
        bool: True if password is correct, False otherwise
    """
    admin_hash = current_app.config['ADMIN_PASSWORD_HASH']
    return check_password_hash(admin_hash, password)


def get_admin_dashboard_data():
    """
    Get data for the admin dashboard.

    Returns:
        dict: Dashboard data
    """
    # Get game state
    game_state = GameState.query.first()

    # Get team stats
    total_teams = Team.query.count()
    pending_teams = Team.query.filter_by(state='pending').count()
    alive_teams = Team.query.filter_by(state='alive').count()
    dead_teams = Team.query.filter_by(state='dead').count()

    # Get player stats
    total_players = Player.query.count()
    alive_players = Player.query.filter_by(state='alive').count()
    dead_players = Player.query.filter_by(state='dead').count()

    # Get kill stats
    total_kills = KillConfirmation.query.filter_by(status='approved').count()
    pending_kills = KillConfirmation.query.filter_by(status='pending').count()

    # Get database stats
    try:
        db_path = current_app.config['SQLALCHEMY_DATABASE_URI'].replace('sqlite:///', '')
        db_size = os.path.getsize(db_path) / (1024 * 1024)  # Size in MB
    except:
        db_size = None

    # Get recent logs
    recent_logs = ActionLog.query.order_by(ActionLog.timestamp.desc()).limit(10).all()

    # Compile dashboard data
    dashboard_data = {
        'game_state': {
            'state': game_state.state,
            'round_number': game_state.round_number,
            'voting_threshold': game_state.voting_threshold,
            'round_start': game_state.round_start,
            'round_end': game_state.round_end
        },
        'team_stats': {
            'total': total_teams,
            'pending': pending_teams,
            'alive': alive_teams,
            'dead': dead_teams
        },
        'player_stats': {
            'total': total_players,
            'alive': alive_players,
            'dead': dead_players
        },
        'kill_stats': {
            'total': total_kills,
            'pending': pending_kills
        },
        'database': {
            'size_mb': db_size
        },
        'recent_logs': [
            {
                'action_type': log.action_type,
                'description': log.description,
                'actor': log.actor,
                'timestamp': log.timestamp
            } for log in recent_logs
        ]
    }

    return dashboard_data


def update_voting_threshold(new_threshold):
    """
    Update the voting threshold for kill confirmations.

    Args:
        new_threshold (int): The new voting threshold

    Returns:
        bool: True if successful, False otherwise
    """
    try:
        # Get game state
        game_state = GameState.query.first()

        if not game_state:
            return False

        # Update threshold
        old_threshold = game_state.voting_threshold
        game_state.voting_threshold = new_threshold

        # Log the action
        log = ActionLog(
            action_type='voting_threshold_change',
            description=f'Voting threshold changed from {old_threshold} to {new_threshold}',
            actor='admin'
        )
        db.session.add(log)

        # Commit changes
        db.session.commit()

        # Log the event
        current_app.logger.info(f'Voting threshold changed from {old_threshold} to {new_threshold}')

        return True
    except Exception as e:
        current_app.logger.error(f'Failed to update voting threshold: {str(e)}')
        db.session.rollback()
        return False


def wipe_game():
    """
    Wipe all game data and reset the game state to pre-game.

    This deletes all teams, players, kill confirmations, and uploaded files.
    It also resets the game state to pre-game and sets the round number to 0.

    Returns:
        bool: True if successful, False otherwise
    """
    try:
        # 1. Delete all database records (in correct order to respect foreign keys)
        # First, delete all kill votes
        KillVote.query.delete()

        # Delete all kill confirmations
        KillConfirmation.query.delete()

        # Delete all players
        Player.query.delete()

        # Delete all teams
        Team.query.delete()

        # 2. Reset game state
        game_state = GameState.query.first()
        if game_state:
            game_state.state = 'pre'
            game_state.round_number = 0
            game_state.round_start = None
            game_state.round_end = None
            # Keep the voting threshold as is

        # 3. Clear uploads directory
        upload_folder = current_app.config['UPLOAD_FOLDER']


        if os.path.exists(upload_folder):
            for filename in os.listdir(upload_folder):
                file_path = os.path.join(upload_folder, filename)
                try:
                    if os.path.isfile(file_path) or os.path.islink(file_path):
                        os.unlink(file_path)
                except Exception as e:
                    current_app.logger.warning(f"Failed to delete file {file_path}: {e}")

        # 4. Add action log
        log = ActionLog(
            action_type='game_wipe',
            description='Game data has been completely wiped and reset',
            actor='admin'
        )
        db.session.add(log)

        # 5. Commit all changes
        db.session.commit()

        current_app.logger.info('Game data has been completely wiped and reset')
        return True

    except Exception as e:
        current_app.logger.error(f'Failed to wipe game data: {str(e)}')
        db.session.rollback()
        return False


def accept_team(team_id):
    """
    Accept a pending team into the game.

    Args:
        team_id (str): ID of the team to accept

    Returns:
        bool: True if successful, False otherwise
    """
    # Get game state
    game_state = GameState.query.first()

    # Can only accept teams before the game starts
    if game_state.state != 'pre':
        return False

    # Get the team
    team = Team.query.get(team_id)
    if not team or team.state != 'pending':
        return False

    # Update team state
    team.state = 'alive'

    # Log the action
    log = ActionLog(
        action_type='team_acceptance',
        description=f'Team {team.name} accepted into the game',
        actor='admin'
    )
    db.session.add(log)

    # Commit changes
    db.session.commit()

    players = Player.query.filter_by(team_id=team.id).all()
    player_names = [player.name for player in players]

    # Send admin email with team photo
    try:
        send_admin_image(
            subject=f"New Team Approved: {team.name}",
            text_body=f"Team Name: {team.name}\n"
                      f"Players: {', '.join(player_names)}",
            image_path=os.path.join(current_app.config['UPLOAD_FOLDER'], team.photo_path.split('/')[1])
        )
    except Exception as e:
        current_app.logger.error(f"Failed to send admin email: {str(e)}")

    # Log the event
    current_app.logger.info(f'Team {team.name} (ID: {team.id}) accepted into the game')

    return True


def change_game_state(new_state):
    """
    Change the game state.

    Args:
        new_state (str): New game state ('pre', 'live', 'post', 'forced')

    Returns:
        bool: True if successful, False otherwise
    """
    # Validate new state
    if new_state not in ['pre', 'live', 'post', 'forced']:
        return False

    # Get game state
    game_state = GameState.query.first()
    old_state = game_state.state

    # Update state
    game_state.state = new_state

    if new_state == 'pre':
        game_state.round_number = 0

    # Special handling for transitioning to 'live'
    if new_state == 'live' and old_state == 'pre':
        # Auto-start round 1
        start_round(increment=True)

    # Special handling for transitioning to 'post'
    if new_state == 'post':
        # Determine the winner
        check_game_complete()

    # Log the action
    log = ActionLog(
        action_type='game_state_change',
        description=f'Game state changed from {old_state} to {new_state}',
        actor='admin'
    )
    db.session.add(log)

    # Commit changes
    db.session.commit()

    # Log the event
    current_app.logger.info(f'Game state changed from {old_state} to {new_state}')

    return True


def start_round(increment=True):
    """
    Start a new game round.

    Args:
        increment (bool): Whether to increment the round number

    Returns:
        tuple: (success, round_number)
    """
    try:
        game_state = GameState.query.first()

        if increment:
            # Revive all players in alive teams for new round
            if game_state.round_number != 0:
                from app.services.game_service import game_logic
                game_logic()

            from app.services.game_service import increment_rounds
            increment_rounds()

        # assigns random targets
        from app.services.game_service import assign_targets
        assign_targets()

        current_app.logger.info(f"Started round {game_state.round_number}")

        # Log the action
        log = ActionLog(
            action_type='round_start',
            description=f'Round {game_state.round_number} started',
            actor='admin'
        )
        db.session.add(log)
        db.session.commit()

        return True, game_state.round_number

    except Exception as e:
        current_app.logger.error(f"Failed to start round: {str(e)}")
        db.session.rollback()
        return False, None


def set_round_schedule(round_start, round_end):
    """
    Set the schedule for the current round.

    Args:
        round_start (datetime): Round start time
        round_end (datetime): Round end time

    Returns:
        bool: True if successful, False otherwise
    """
    # Get game state
    game_state = GameState.query.first()

    # Update schedule
    game_state.round_start = round_start
    game_state.round_end = round_end

    # Log the action
    log = ActionLog(
        action_type='round_schedule',
        description=f'Round {game_state.round_number} schedule set: Start={round_start}, End={round_end}',
        actor='admin'
    )
    db.session.add(log)

    # Commit changes
    db.session.commit()

    # Log the event
    current_app.logger.info(f'Round {game_state.round_number} schedule set: Start={round_start}, End={round_end}')

    # Update scheduler
    from app.services.game_service import schedule_round_transitions
    schedule_round_transitions(current_app._get_current_object())

    return True


def toggle_team_state(team_id):
    """
    Toggle a team's state between alive and dead.

    Args:
        team_id (str): ID of the team to toggle

    Returns:
        bool: True if successful, False otherwise
    """
    # Get the team
    team = Team.query.get(team_id)
    if not team:
        return False

    # Toggle state
    if team.state == 'alive':
        team.state = 'dead'
        action = 'killed'

        # Mark all players as dead
        for player in team.players:
            player.state = 'dead'
    else:
        team.state = 'alive'
        action = 'revived'

        # Mark all players as alive
        for player in team.players:
            player.state = 'alive'

    # Log the action
    log = ActionLog(
        action_type=f'team_{action}',
        description=f'Team {team.name} {action} by admin',
        actor='admin'
    )
    db.session.add(log)

    # Commit changes
    db.session.commit()

    # Log the event
    current_app.logger.info(f'Team {team.name} (ID: {team.id}) {action} by admin')

    return True


def toggle_player_state(player_id):
    """
    Toggle a player's state between alive and dead.

    Args:
        player_id (str): ID of the player to toggle

    Returns:
        bool: True if successful, False otherwise
    """
    # Get the player
    player = Player.query.get(player_id)
    if not player:
        return False

    # Toggle state
    if player.is_alive:
        player.state = 'dead'
        action = 'killed'

        # Check if team is now dead
        team = Team.query.get(player.team_id)
        if all(not p.is_alive for p in team.players):
            team.state = 'dead'
    else:
        player.state = 'alive'
        action = 'revived'

        # Ensure team is alive
        team = Team.query.get(player.team_id)
        team.state = 'alive'

    # Log the action
    log = ActionLog(
        action_type=f'player_{action}',
        description=f'Player {player.name} {action} by admin',
        actor='admin'
    )
    db.session.add(log)

    # Commit changes
    db.session.commit()

    # Log the event
    current_app.logger.info(f'Player {player.name} (ID: {player.id}) {action} by admin')

    return True


def force_vote_decision(kill_confirmation_id, decision):
    """
    Force a decision on a kill confirmation vote.

    Args:
        kill_confirmation_id (str): ID of the kill confirmation
        decision (bool): True to approve, False to reject

    Returns:
        bool: True if successful, False otherwise
    """
    # Get the kill confirmation
    kill_confirmation = KillConfirmation.query.get(kill_confirmation_id)
    if not kill_confirmation or not kill_confirmation.is_pending:
        return False

    # Update status
    kill_confirmation.status = 'approved' if decision else 'rejected'

    # If approved, mark the victim as dead
    if decision:
        from app.services.game_service import confirm_kill
        confirm_kill(kill_confirmation)

    # Log the action
    log = ActionLog(
        action_type='vote_override',
        description=f'Kill confirmation {kill_confirmation_id} {"approved" if decision else "rejected"} by admin',
        actor='admin'
    )
    db.session.add(log)

    # Commit changes
    db.session.commit()

    # Log the event
    current_app.logger.info(
        f'Kill confirmation {kill_confirmation_id} {"approved" if decision else "rejected"} by admin')

    return True


def backup_database(app=None):
    """
    Backup the SQLite database to a file.

    Args:
        app: Flask app object (optional)

    Returns:
        str: Path to the backup file, or None if failed
    """
    if app:
        # Use the provided app context
        with app.app_context():
            return _do_backup()
    else:
        # Use current app context
        return _do_backup()


def _do_backup():
    """
    Internal function to perform the actual database backup.

    Returns:
        str: Path to the backup file, or None if failed
    """
    try:
        # Get database path from config
        db_uri = current_app.config['SQLALCHEMY_DATABASE_URI']
        if db_uri.startswith('sqlite:///'):
            db_path = db_uri[10:]
        else:
            # Not SQLite, use pg_dump for PostgreSQL or similar
            return None

        # Ensure the backup directory exists
        backup_dir = current_app.config['BACKUP_DIR']
        os.makedirs(backup_dir, exist_ok=True)

        # Create backup filename with timestamp
        timestamp = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
        backup_path = os.path.join(backup_dir, f'backup_{timestamp}.db')

        # Create backup
        shutil.copy2(db_path, backup_path)

        # Log the backup
        log = ActionLog(
            action_type='database_backup',
            description=f'Database backed up to {backup_path}',
            actor='system'
        )
        db.session.add(log)
        db.session.commit()

        # Log the event
        current_app.logger.info(f'Database backed up to {backup_path}')

        return backup_path

    except Exception as e:
        current_app.logger.error(f'Database backup failed: {str(e)}')
        return None


def execute_db_command(sql_command):
    """
    Execute a raw SQL command on the database.

    Args:
        sql_command (str): SQL command to execute

    Returns:
        tuple: (success, result)
    """
    try:
        # Execute the command
        result = db.session.execute(text(sql_command))
        db.session.commit()

        # Convert result to a list of dicts
        if result.returns_rows:
            columns = result.keys()
            rows = []
            for row in result:
                rows.append(dict(zip(columns, row)))
            result_data = rows
        else:
            result_data = {'rowcount': result.rowcount}

        # Log the action
        log = ActionLog(
            action_type='db_command',
            description=f'SQL command executed: {sql_command[:100]}...' if len(
                sql_command) > 100 else f'SQL command executed: {sql_command}',
            actor='admin'
        )
        db.session.add(log)
        db.session.commit()

        # Log the event
        current_app.logger.info(f'SQL command executed: {sql_command}')

        return True, result_data

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f'SQL command failed: {str(e)}')
        return False, str(e)


def toggle_voting_status():
    """Toggle the voting feature on/off."""
    try:
        game_state = GameState.query.first()
        game_state.voting_enabled = not game_state.voting_enabled
        db.session.commit()

        # Log the action
        log = ActionLog(
            action_type='voting_toggle',
            description=f'Voting has been {"enabled" if game_state.voting_enabled else "disabled"}',
            actor='admin'
        )
        db.session.add(log)
        db.session.commit()

        return True, game_state.voting_enabled
    except Exception as e:
        current_app.logger.error(f"Failed to toggle voting status: {str(e)}")
        return False, None


def toggle_free_for_all(enable):
    """Toggle free-for-all mode and update target assignments."""
    try:
        # Get game state
        game_state = GameState.query.first()
        if not game_state:
            return False

        # Update free-for-all flag
        old_state = game_state.free_for_all
        game_state.free_for_all = enable

        if enable:
            # In free-for-all mode, we'll maintain existing targets for reference
            # but the free_for_all flag will indicate that anyone can target anyone
            alive_players = Player.query.filter_by(state='alive').all()
            current_app.logger.info(f"Free-for-all mode enabled: {len(alive_players)} players can target anyone")

            log = ActionLog(
                action_type='free_for_all',
                description=f'Free for all mode enabled - all {len(alive_players)} players can target anyone',
                actor='admin'
            )
            db.session.add(log)
        else:
            # Reassign targets normally if disabling free-for-all
            from app.services.game_service import assign_targets
            assign_targets()

            log = ActionLog(
                action_type='free_for_all',
                description='Free for all mode disabled - normal targeting restored',
                actor='admin'
            )
            db.session.add(log)

        db.session.commit()
        return True
    except Exception as e:
        current_app.logger.error(f"Error toggling free-for-all mode: {str(e)}")
        db.session.rollback()
        return False

def deny_team(team_id):
    """
    Deny a team's registration by deleting the team and its players.

    Args:
        team_id: The ID of the team to deny

    Returns:
        tuple: (success, team_name or error message)
    """
    team = Team.query.get(team_id)
    if not team:
        return False, "Team not found"

    # Get team name before deletion for the message
    team_name = team.name

    try:
        # First delete the associated players
        Player.query.filter_by(team_id=team_id).delete()

        # Then delete the team
        db.session.delete(team)
        db.session.commit()

        # Log the action
        log = ActionLog(
            action_type='deny_team',
            description=f'Deny team "{team_name}"',
            actor='admin'
        )
        db.session.add(log)

        return True, team_name
    except Exception as e:
        db.session.rollback()
        return False, str(e)


def send_mass_email_service(subject, content):
    """
    Send a single email to all players with their email addresses visible to each other.

    Args:
        subject (str): Email subject
        content (str): Email content

    Returns:
        tuple: (success, message)
    """
    try:
        # Get all players with email addresses
        players = Player.query.filter(Player.email != None).all()
        if not players:
            current_app.logger.warning("No players with email addresses found")
            return False, "No players with email addresses found"

        # Import email service
        from app.services.email_service import send_email

        # Collect all email addresses
        email_addresses = [player.email for player in players if player.email]

        if not email_addresses:
            return False, "No valid email addresses found"

        # Send a single email with all recipients in TO field
        send_email(
            subject=subject,
            recipients=email_addresses,  # Pass the list of emails directly
            text_body=content,
            html_body=None
        )

        current_app.logger.info(f"Mass email sent to {len(email_addresses)} players: {subject}")

        # Log the action
        log = ActionLog(
            action_type='mass_email',
            description=f'Mass email sent to {len(email_addresses)} players: {subject}',
            actor='admin'
        )
        db.session.add(log)
        db.session.commit()

        return True, f"Email sent successfully to {len(email_addresses)} players"
    except Exception as e:
        current_app.logger.error(f"Failed to send mass email: {str(e)}")
        return False, str(e)
