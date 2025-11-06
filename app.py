"""RL Analyzer Application

This module implements a simple HTTP server using the built‑in
``http.server`` module.  The server provides a handful of routes for
uploading Rocket League match data, viewing a player's profile and
statistics, and offering coaching advice based on differences between
the player's performance and that of top ranked streamers.  A small
SQLite database (``database.db``) is used to persist user data and
match statistics.

The goal of this module is not to be production ready but rather to
demonstrate the core concepts of the proposed RL Analyzer in an
environment where third‑party web frameworks (such as Flask or
Django) are unavailable.  The server uses very simple templating
based on Python's ``string.Template`` class to render HTML pages.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from http.server import HTTPServer, BaseHTTPRequestHandler
from io import BytesIO
from urllib.parse import parse_qs, urlparse
from typing import Dict, Any, Tuple, Optional
from string import Template


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATABASE_PATH = os.path.join(BASE_DIR, "database.db")
TEMPLATE_DIR = os.path.join(BASE_DIR, "templates")
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")

# Ensure necessary directories exist
os.makedirs(UPLOAD_DIR, exist_ok=True)


def init_db(path: str = DATABASE_PATH) -> None:
    """Initialise the SQLite database.

    This function creates the necessary tables if they do not already
    exist.  It also inserts a default user and a handful of dummy
    streamer statistics to allow the application to function without
    external dependencies.

    Tables:
      users (id INTEGER PRIMARY KEY, username TEXT, rank TEXT,
             followers INTEGER, friends INTEGER)
      matches (id INTEGER PRIMARY KEY, user_id INTEGER,
               file_path TEXT, metrics TEXT)
      streamers (id INTEGER PRIMARY KEY, name TEXT, rank TEXT,
                metrics TEXT)

    The ``metrics`` columns store JSON encoded dictionaries of
    aggregated statistics.  See ``parse_match_data`` for the metrics
    collected from match uploads.
    """
    conn = sqlite3.connect(path)
    c = conn.cursor()
    # Create tables
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE,
            rank TEXT,
            followers INTEGER DEFAULT 0,
            friends INTEGER DEFAULT 0
        )
        """
    )
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS matches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            file_path TEXT,
            metrics TEXT,
            FOREIGN KEY(user_id) REFERENCES users(id)
        )
        """
    )
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS streamers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT,
            rank TEXT,
            metrics TEXT
        )
        """
    )
    # Create a default user if none exists
    c.execute("SELECT id FROM users LIMIT 1")
    row = c.fetchone()
    if row is None:
        c.execute(
            "INSERT INTO users (username, rank, followers, friends) VALUES (?, ?, ?, ?)",
            ("Player1", "Gold", 0, 0),
        )
    # Insert dummy streamer metrics if table is empty
    c.execute("SELECT id FROM streamers LIMIT 1")
    if c.fetchone() is None:
        # Each entry represents average metrics for a streamer at a given rank
        dummy_streamers: list[Tuple[str, str, Dict[str, float]]] = [
            ("StreamerAlpha", "Bronze", {"boost_usage": 0.30, "flip_count": 5, "shots": 1, "goals": 0}),
            ("StreamerBeta", "Silver", {"boost_usage": 0.35, "flip_count": 7, "shots": 2, "goals": 0.2}),
            ("StreamerGamma", "Gold", {"boost_usage": 0.40, "flip_count": 9, "shots": 3, "goals": 0.4}),
            ("StreamerDelta", "Platinum", {"boost_usage": 0.45, "flip_count": 11, "shots": 3.5, "goals": 0.6}),
            ("StreamerEpsilon", "Diamond", {"boost_usage": 0.50, "flip_count": 13, "shots": 4, "goals": 0.8}),
        ]
        for name, rank, metrics in dummy_streamers:
            c.execute(
                "INSERT INTO streamers (name, rank, metrics) VALUES (?, ?, ?)",
                (name, rank, json.dumps(metrics)),
            )
    conn.commit()
    conn.close()


def load_template(template_name: str) -> Template:
    """Load and return a Template from the templates directory."""
    path = os.path.join(TEMPLATE_DIR, template_name)
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()
    return Template(content)


def parse_match_data(file_path: str) -> Dict[str, float]:
    """Parse a JSON file representing match data and extract metrics.

    The expected structure of the JSON file is flexible.  This function
    looks for a top‑level dictionary containing a list of button
    ``events`` where each event may have a ``button`` key and
    associated ``action``.  It also accepts the following optional
    fields:

      * ``boost_frames`` – number of frames where boost was held
      * ``total_frames`` – total number of frames in the replay
      * ``shots`` – number of shots taken
      * ``goals`` – number of goals scored

    If these keys are missing they are derived from the events list
    where possible.  The parser is intentionally forgiving to allow
    experimentation with different replay export formats.  Unknown
    structures simply yield zero metrics.
    """
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        # If the file is not valid JSON, return empty metrics
        return {"boost_usage": 0.0, "flip_count": 0.0, "shots": 0.0, "goals": 0.0}

    events = data.get("events", [])
    boost_frames = data.get("boost_frames")
    total_frames = data.get("total_frames")
    shots = data.get("shots")
    goals = data.get("goals")

    # Derive metrics from events list if explicit values not provided
    flip_count = 0
    if events:
        for event in events:
            if isinstance(event, dict):
                btn = str(event.get("button", "")).lower()
                action = str(event.get("action", "")).lower()
                if btn in {"flip", "double_jump", "jump_flip"} or action in {"flip", "double_jump"}:
                    flip_count += 1
                if boost_frames is None:
                    # Count boost events if no explicit boost count provided
                    if btn == "boost" or action == "boost":
                        boost_frames = (boost_frames or 0) + 1
    # Default frames if not provided
    if total_frames is None:
        total_frames = max(len(events), 1)
    if boost_frames is None:
        boost_frames = 0
    # Shots and goals defaults
    shots = shots if isinstance(shots, (int, float)) else 0
    goals = goals if isinstance(goals, (int, float)) else 0
    # Compute boost usage ratio
    boost_usage_ratio = float(boost_frames) / float(total_frames) if total_frames else 0.0
    return {
        "boost_usage": round(boost_usage_ratio, 3),
        "flip_count": float(flip_count),
        "shots": float(shots),
        "goals": float(goals),
    }


def aggregate_user_metrics(user_id: int, conn: sqlite3.Connection) -> Dict[str, float]:
    """Aggregate metrics across all matches for a user.

    Returns a dictionary mapping metric names to averaged values.  If the
    user has no matches the dictionary contains zeros.  The metrics
    computed mirror those produced by ``parse_match_data``.
    """
    c = conn.cursor()
    c.execute("SELECT metrics FROM matches WHERE user_id = ?", (user_id,))
    rows = c.fetchall()
    if not rows:
        return {"boost_usage": 0.0, "flip_count": 0.0, "shots": 0.0, "goals": 0.0}
    totals: Dict[str, float] = {"boost_usage": 0.0, "flip_count": 0.0, "shots": 0.0, "goals": 0.0}
    for (metrics_json,) in rows:
        try:
            metrics = json.loads(metrics_json)
        except Exception:
            continue
        for k in totals:
            totals[k] += float(metrics.get(k, 0.0))
    count = len(rows)
    return {k: round(v / count, 3) for k, v in totals.items()}


def get_streamer_average(rank: str, conn: sqlite3.Connection) -> Dict[str, float]:
    """Compute the average metrics for streamers at the specified rank."""
    c = conn.cursor()
    c.execute("SELECT metrics FROM streamers WHERE rank = ?", (rank,))
    rows = c.fetchall()
    if not rows:
        # If no streamers exist for the rank, return zeros
        return {"boost_usage": 0.0, "flip_count": 0.0, "shots": 0.0, "goals": 0.0}
    totals = {"boost_usage": 0.0, "flip_count": 0.0, "shots": 0.0, "goals": 0.0}
    for (metrics_json,) in rows:
        try:
            metrics = json.loads(metrics_json)
        except Exception:
            continue
        for k in totals:
            totals[k] += float(metrics.get(k, 0.0))
    count = len(rows)
    return {k: round(v / count, 3) for k, v in totals.items()}


def compute_strengths_and_weaknesses(user_metrics: Dict[str, float], streamer_avg: Dict[str, float]) -> Tuple[Dict[str, float], Dict[str, float], Optional[str]]:
    """Determine strengths, weaknesses and biggest discrepancy.

    Strengths are metrics where the user's value is strictly greater
    than the streamer average.  Weaknesses are those where the user's
    value is strictly lower.  The biggest discrepancy is the metric
    with the largest absolute difference.

    Returns a tuple (strengths, weaknesses, biggest_diff_key).
    """
    strengths = {}
    weaknesses = {}
    biggest_key = None
    biggest_delta = 0.0
    for key in user_metrics:
        user_val = user_metrics[key]
        avg_val = streamer_avg.get(key, 0.0)
        delta = user_val - avg_val
        if delta > 0:
            strengths[key] = round(delta, 3)
        elif delta < 0:
            weaknesses[key] = round(-delta, 3)
        # Track largest absolute difference
        if abs(delta) > biggest_delta:
            biggest_delta = abs(delta)
            biggest_key = key
    return strengths, weaknesses, biggest_key


def generate_tips(strengths: Dict[str, float], weaknesses: Dict[str, float]) -> Tuple[list[str], list[str]]:
    """Generate simple coaching tips based on strengths and weaknesses.

    Short term tips focus on weaknesses (things to practice right away),
    while long term tips encourage maintaining strengths and building
    consistency.
    """
    short_term = []
    long_term = []
    for k in weaknesses:
        if k == "boost_usage":
            short_term.append("Work on managing your boost more efficiently. Try to use small pads to stay stocked.")
        elif k == "flip_count":
            short_term.append("Practice aerial control and flipping mechanics in free play to increase your flip count.")
        elif k == "shots":
            short_term.append("Focus on taking more shots during matches. Position yourself to create shooting opportunities.")
        elif k == "goals":
            short_term.append("Work on finishing plays and accuracy to convert shots into goals.")
    for k in strengths:
        if k == "boost_usage":
            long_term.append("Continue to refine your boost management. Use efficient routes to maintain high boost levels.")
        elif k == "flip_count":
            long_term.append("Keep practicing advanced movement like wave dashes and flip resets to stay ahead.")
        elif k == "shots":
            long_term.append("Maintain your shooting pace; practice different angles and power shots.")
        elif k == "goals":
            long_term.append("Keep improving goal conversion by working on placement and deception.")
    return short_term, long_term


class RLAnalyzerRequestHandler(BaseHTTPRequestHandler):
    """Request handler for the RL Analyzer HTTP server."""

    def do_GET(self) -> None:
        """Handle GET requests by routing to the appropriate page."""
        parsed = urlparse(self.path)
        route = parsed.path
        if route in {"/", "/home"}:
            self.render_home()
        elif route == "/profile":
            self.render_profile()
        elif route == "/coach":
            self.render_coach()
        elif route == "/upload":
            self.render_upload_page()
        elif route.startswith("/static/"):
            self.serve_static(route)
        else:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"404 Not Found")

    def do_POST(self) -> None:
        """Handle POST requests for file uploads."""
        parsed = urlparse(self.path)
        if parsed.path == "/upload":
            self.handle_upload()
        else:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"404 Not Found")

    # Utility methods
    def get_default_user_id(self) -> int:
        """Return the first user's ID.  In a real application this would
        come from authentication, but here we use the single default user.
        """
        conn = sqlite3.connect(DATABASE_PATH)
        c = conn.cursor()
        c.execute("SELECT id FROM users ORDER BY id LIMIT 1")
        row = c.fetchone()
        conn.close()
        return row[0] if row else 1

    def render_template(self, template_name: str, context: Dict[str, Any]) -> bytes:
        """Render an HTML template with the given context."""
        template = load_template(template_name)
        # Convert context values to strings so Template can substitute
        safe_context = {k: (str(v) if not isinstance(v, str) else v) for k, v in context.items()}
        content = template.safe_substitute(**safe_context)
        return content.encode("utf-8")

    def render_home(self) -> None:
        """Render the home page."""
        body = self.render_template(
            "home.html",
            {
                "title": "RL Analyzer – Home",
            },
        )
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def render_profile(self) -> None:
        """Render the profile page for the default user."""
        user_id = self.get_default_user_id()
        conn = sqlite3.connect(DATABASE_PATH)
        c = conn.cursor()
        c.execute("SELECT username, rank, followers, friends FROM users WHERE id = ?", (user_id,))
        row = c.fetchone()
        if row:
            username, rank, followers, friends = row
        else:
            username, rank, followers, friends = ("Unknown", "Unranked", 0, 0)
        metrics = aggregate_user_metrics(user_id, conn)
        # Determine top games by selecting matches with highest goals
        c.execute("SELECT id, metrics FROM matches WHERE user_id = ?", (user_id,))
        match_rows = c.fetchall()
        top_games_list = []
        for match_id, metrics_json in match_rows:
            try:
                m = json.loads(metrics_json)
                top_games_list.append((match_id, m.get("goals", 0)))
            except Exception:
                continue
        # Sort by goals descending and take up to 5 matches
        top_games_list.sort(key=lambda x: x[1], reverse=True)
        top_games_str = ", ".join([f"Game {mid} (goals: {int(goals)})" for mid, goals in top_games_list[:5]]) or "None"
        context = {
            "title": f"{username}'s Profile",
            "username": username,
            "rank": rank,
            "followers": followers,
            "friends": friends,
            "boost_usage": metrics.get("boost_usage", 0.0),
            "flip_count": metrics.get("flip_count", 0.0),
            "shots": metrics.get("shots", 0.0),
            "goals": metrics.get("goals", 0.0),
            "top_games": top_games_str,
        }
        body = self.render_template("profile.html", context)
        conn.close()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def render_coach(self) -> None:
        """Render the personal coach page."""
        user_id = self.get_default_user_id()
        conn = sqlite3.connect(DATABASE_PATH)
        # Fetch user rank
        c = conn.cursor()
        c.execute("SELECT rank FROM users WHERE id = ?", (user_id,))
        row = c.fetchone()
        user_rank = row[0] if row else "Unranked"
        user_metrics = aggregate_user_metrics(user_id, conn)
        streamer_avg = get_streamer_average(user_rank, conn)
        strengths, weaknesses, biggest = compute_strengths_and_weaknesses(user_metrics, streamer_avg)
        short_term, long_term = generate_tips(strengths, weaknesses)
        context = {
            "title": "Personal Coach",
            "rank": user_rank,
            "biggest_diff": biggest or "N/A",
            "strengths_list": ", ".join([f"{k} (+{v})" for k, v in strengths.items()]) or "None",
            "weaknesses_list": ", ".join([f"{k} (-{v})" for k, v in weaknesses.items()]) or "None",
            "short_tips": "<br/>".join(short_term) or "No immediate suggestions.",
            "long_tips": "<br/>".join(long_term) or "No long term suggestions.",
        }
        body = self.render_template("coach.html", context)
        conn.close()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def render_upload_page(self) -> None:
        """Render a page with a form to upload a match file."""
        context = {
            "title": "Upload Match",
        }
        body = self.render_template("upload.html", context)
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def handle_upload(self) -> None:
        """Handle POST request for uploading a match replay file."""
        content_length = int(self.headers.get('Content-Length', 0))
        content_type = self.headers.get('Content-Type', '')
        if 'multipart/form-data' not in content_type:
            # Only support multipart form uploads
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b"Unsupported content type")
            return
        # Parse the multipart form data
        # We use cgi.FieldStorage to handle file uploads conveniently
        import cgi
        environ = {
            'REQUEST_METHOD': 'POST',
            'CONTENT_TYPE': content_type,
            'CONTENT_LENGTH': str(content_length),
        }
        form = cgi.FieldStorage(fp=self.rfile, headers=self.headers, environ=environ)
        # Retrieve the uploaded file object from the form.  The FieldStorage
        # object does not support boolean evaluation directly, so avoid
        # using it in a boolean context.  Instead test the presence of
        # the key and ensure the underlying file attribute exists.
        if 'match_file' not in form or getattr(form['match_file'], 'file', None) is None:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b"No file uploaded")
            return
        file_field = form['match_file']
        # Save uploaded file
        filename = os.path.basename(file_field.filename or 'match.json')
        save_path = os.path.join(UPLOAD_DIR, filename)
        with open(save_path, 'wb') as f:
            f.write(file_field.file.read())
        # Parse metrics
        metrics = parse_match_data(save_path)
        # Insert match into database
        user_id = self.get_default_user_id()
        conn = sqlite3.connect(DATABASE_PATH)
        c = conn.cursor()
        c.execute(
            "INSERT INTO matches (user_id, file_path, metrics) VALUES (?, ?, ?)",
            (user_id, save_path, json.dumps(metrics)),
        )
        conn.commit()
        conn.close()
        # Redirect back to profile
        self.send_response(303)
        self.send_header('Location', '/profile')
        self.end_headers()

    def serve_static(self, route: str) -> None:
        """Serve static files such as CSS.  Only serves files from the
        ``static`` directory inside the repository to avoid leaking
        arbitrary filesystem contents.
        """
        # Remove '/static/' prefix
        rel_path = route[len('/static/'):]
        static_dir = os.path.join(BASE_DIR, 'static')
        file_path = os.path.join(static_dir, rel_path)
        if not os.path.abspath(file_path).startswith(os.path.abspath(static_dir)):
            self.send_response(403)
            self.end_headers()
            self.wfile.write(b"Forbidden")
            return
        if not os.path.exists(file_path):
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"Not Found")
            return
        # Determine content type
        if file_path.endswith('.css'):
            mime = 'text/css'
        elif file_path.endswith('.js'):
            mime = 'application/javascript'
        else:
            mime = 'application/octet-stream'
        with open(file_path, 'rb') as f:
            content = f.read()
        self.send_response(200)
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', str(len(content)))
        self.end_headers()
        self.wfile.write(content)


def run_server(host: str = '0.0.0.0', port: int = 8000) -> None:
    """Initialise the database and start the HTTP server."""
    init_db()
    server_address = (host, port)
    httpd = HTTPServer(server_address, RLAnalyzerRequestHandler)
    print(f"Starting RL Analyzer server at http://{host}:{port}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        print("Server stopped.")


if __name__ == '__main__':
    # If run directly, start the server
    run_server()