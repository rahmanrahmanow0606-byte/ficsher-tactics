
import io
import os
import math
import shutil
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

try:
    import chess
    import chess.pgn
    import chess.engine
except ImportError:
    raise SystemExit("Install dependencies: py -m pip install python-chess")


# ============================================================
# CONFIGURATION
# ============================================================

ENGINE_PATH = ""
DEFAULT_DEPTH = 16
DEFAULT_RATING = 1200

PIECE_VALUES = {
    chess.PAWN: 1,
    chess.KNIGHT: 3,
    chess.BISHOP: 3,
    chess.ROOK: 5,
    chess.QUEEN: 9,
    chess.KING: 0,
}

PIECES = {
    "K": "♔", "Q": "♕", "R": "♖",
    "B": "♗", "N": "♘", "P": "♙",
    "k": "♚", "q": "♛", "r": "♜",
    "b": "♝", "n": "♞", "p": "♟",
}

# Chess.com-inspired move classification palette.
# Chess.com does not publish official HEX values; these are close visual matches.
COLORS = {
    "BRILLIANT": "#1BAAA0",   # Teal
    "GREAT": "#5B9BD5",       # Blue
    "BEST": "#5C8A3C",        # Dark green
    "EXCELLENT": "#96BC4B",   # Lime green
    "GOOD": "#A8C47A",        # Light green
    "INACCURACY": "#F0C15A",  # Yellow
    "MISTAKE": "#E58C2A",     # Orange
    "BLUNDER": "#C03030",     # Red
    "MISS": "#C77BD4",        # Pink / purple
}


# ============================================================
# STOCKFISH
# ============================================================

def find_engine():
    if ENGINE_PATH and os.path.isfile(ENGINE_PATH):
        return ENGINE_PATH

    found = shutil.which("stockfish")
    if found:
        return found

    candidates = [
        "C:/Program Files/Stockfish/stockfish.exe",
        "C:/stockfish/stockfish.exe",
        os.path.expanduser("~/Downloads/stockfish/stockfish.exe"),
        os.path.expanduser("~/Downloads/stockfish.exe"),
    ]

    for path in candidates:
        if os.path.isfile(path):
            return path

    return ""


def score_cp(score, color):
    score = score.pov(color)

    if score.is_mate():
        mate = score.mate()
        return 100000 if mate and mate > 0 else -100000

    return score.score()


# ============================================================
# EXPECTED POINTS MODEL
# ============================================================

def expected_points_from_cp(cp):
    """
    Convert centipawn evaluation into approximate expected points.

    This is a smooth logistic approximation.
    It is NOT Chess.com's proprietary expected-points formula.
    """

    if cp >= 100000:
        return 1.0

    if cp <= -100000:
        return 0.0

    # Avoid numerical overflow.
    cp = max(-10000, min(10000, cp))

    return 1.0 / (1.0 + math.exp(-cp / 400.0))


def classify_move(
    expected_points_lost,
    brilliant=False,
    great=False,
    miss=False
):
    if brilliant:
        return "BRILLIANT"

    if great:
        return "GREAT"

    if miss:
        return "MISS"

    loss = max(0.0, expected_points_lost)

    if loss < 0.005:
        return "BEST"
    elif loss < 0.02:
        return "EXCELLENT"
    elif loss < 0.05:
        return "GOOD"
    elif loss < 0.10:
        return "INACCURACY"
    elif loss < 0.20:
        return "MISTAKE"
    else:
        return "BLUNDER"


# ============================================================
# RATING AND MATERIAL HELPERS
# ============================================================

def get_rating(game, header, fallback=DEFAULT_RATING):
    try:
        rating = int(game.headers.get(header, fallback))

        if 100 <= rating <= 4000:
            return rating

    except (ValueError, TypeError):
        pass

    return fallback


def calculate_game_strengths(rows):
    """
    Experimental head-to-head performance estimate.

    PGN Elo headers provide the starting-rating baseline. Stockfish's
    expected-points loss measures how cleanly each player handled their
    moves. The relative quality difference shifts the two estimates around
    the average starting rating.

    This is deliberately labeled an estimate: Chess.com has not published
    the calibration needed to reproduce its Game Rating exactly.
    """
    player_data = {}

    for player in ("White", "Black"):
        player_rows = [
            row for row in rows
            if row.get("player") == player and not row.get("forced", False)
        ]
        if len(player_rows) < 8:
            return None, len(player_rows), None, 0, None, None

        losses = [
            max(0.0, float(row.get("quality_loss", 0.0)))
            for row in player_rows
        ]
        average_loss = sum(losses) / len(losses)
        ratings = [
            row.get("rating") for row in player_rows
            if isinstance(row.get("rating"), int)
            and 100 <= row["rating"] <= 4000
        ]
        player_data[player] = {
            "count": len(player_rows),
            "loss": average_loss,
            "rating": round(sum(ratings) / len(ratings)) if ratings else DEFAULT_RATING,
        }

    white = player_data["White"]
    black = player_data["Black"]

    # Convert average EP loss into a smooth quality index. Comparing the
    # two players reduces (but does not eliminate) position-specific noise.
    white_quality = 1.0 / (1.0 + 10.0 * white["loss"])
    black_quality = 1.0 / (1.0 + 10.0 * black["loss"])

    quality_gap = 400.0 * math.log10(
        max(0.01, white_quality) / max(0.01, black_quality)
    )
    quality_gap = max(-300.0, min(300.0, quality_gap))

    # Center the estimates on both players' supplied starting ratings.
    baseline = (white["rating"] + black["rating"]) / 2.0
    white_estimate = round(max(100, min(3000, baseline + quality_gap / 2.0)))
    black_estimate = round(max(100, min(3000, baseline - quality_gap / 2.0)))

    return (
        white_estimate, white["count"], black_estimate,
        black["count"], white["loss"], black["loss"]
    )


def material(board, color):
    total = 0

    for piece_type, value in PIECE_VALUES.items():
        total += (
            len(board.pieces(piece_type, color)) * value
        )

    return total


def material_difference(board, color):
    return material(board, color) - material(board, not color)


def is_capture_of_moved_piece(board_after, move):
    """
    Checks whether the opponent can legally capture the piece
    that just moved.

    The opponent does not have to actually play that capture.
    """

    for reply in board_after.legal_moves:
        if reply.to_square != move.to_square:
            continue

        if board_after.is_capture(reply):
            return True

    return False


# ============================================================
# SPECIAL MOVE DETECTION
# ============================================================

def detect_brilliant(
    engine,
    board_before,
    board_after,
    move,
    best_move,
    before_score,
    after_score,
    mover,
    depth
):
    """
    Strict Brilliant-move heuristic.

    Only considers a move when:
    - It is Stockfish's top move.
    - A non-pawn piece worth at least a minor piece is genuinely offered.
    - The opponent's best reply actually captures that piece.
    - The resulting position is still clearly sound for the sacrificing side.

    This intentionally prefers false negatives over false positives.
    It is an approximation, not Chess.com's proprietary classifier.
    """

    # A Brilliant candidate must also be Stockfish's top choice.
    if best_move is None or best_move != move:
        return False

    if board_before.legal_moves.count() <= 1:
        return False

    piece = board_before.piece_at(move.from_square)

    # Pawn moves and trivial king moves are not treated as piece sacrifices.
    if piece is None or piece.piece_type not in (
        chess.KNIGHT,
        chess.BISHOP,
        chess.ROOK,
        chess.QUEEN
    ):
        return False

    # Avoid labeling moves in positions that are already comfortably won.
    before_expected = expected_points_from_cp(before_score)
    after_expected = expected_points_from_cp(after_score)

    if before_expected >= 0.90:
        return False

    # The move itself must not noticeably worsen the evaluation.
    if before_expected - after_expected > 0.015:
        return False

    # The moved piece must be legally capturable on its destination square.
    possible_captures = [
        reply for reply in board_after.legal_moves
        if reply.to_square == move.to_square
        and board_after.is_capture(reply)
    ]

    if not possible_captures:
        return False

    # Do not call it Brilliant if the opponent has a better reply that
    # does not take the offered piece. The top engine PV must capture it.
    try:
        opponent_info = engine.analyse(
            board_after,
            chess.engine.Limit(depth=max(10, depth))
        )
    except (chess.engine.EngineError, chess.engine.EngineTerminatedError):
        return False

    opponent_pv = opponent_info.get("pv") or []
    if not opponent_pv:
        return False

    best_reply = opponent_pv[0]

    if (
        best_reply.to_square != move.to_square
        or not board_after.is_capture(best_reply)
    ):
        return False

    # Evaluate the position after the opponent accepts the sacrifice.
    captured_position = board_after.copy()
    captured_position.push(best_reply)

    # Check the actual material result of the entire sequence:
    # our move followed by the opponent accepting the offered piece.
    # A piece being captured is NOT automatically a sacrifice. For
    # example, Nxb8 Qxb8 wins a rook for a knight (+2), while
    # Rxh8 Kxh8 is only an equal rook trade (0).
    material_swing = (
        material_difference(captured_position, mover)
        - material_difference(board_before, mover)
    )

    # Require a meaningful net material investment. This rejects
    # ordinary exchanges and captures that win more material than
    # the sacrificed piece is worth.
    if material_swing > -1.5:
        return False

    try:
        captured_info = engine.analyse(
            captured_position,
            chess.engine.Limit(depth=max(10, depth))
        )
    except (chess.engine.EngineError, chess.engine.EngineTerminatedError):
        return False

    captured_score = score_cp(captured_info["score"], mover)
    captured_expected = expected_points_from_cp(captured_score)

    # The sacrifice must leave a genuinely playable, preferably favorable
    # position even after the opponent takes the offered piece.
    if captured_expected < 0.58:
        return False

    # Also reject sacrifices where accepting the piece causes a substantial
    # drop relative to the position immediately after the move.
    if after_expected - captured_expected > 0.12:
        return False

    return True


def detect_great(
    engine,
    board_before,
    move,
    best_move,
    before_expected,
    after_expected,
    mover,
    depth
):
    """
    Heuristic Great detection.

    Looks for a critical position where the played move is the
    engine's best move and significantly improves the position.
    """

    if best_move != move:
        return False

    if board_before.legal_moves.count() <= 1:
        return False

    improvement = after_expected - before_expected

    if improvement < 0.20:
        return False

    # Check whether alternative moves are substantially worse.
    alternatives = []

    for alternative in board_before.legal_moves:
        if alternative == move:
            continue

        test_board = board_before.copy()
        test_board.push(alternative)

        info = engine.analyse(
            test_board,
            chess.engine.Limit(depth=max(8, depth - 2))
        )

        alternative_score = score_cp(info["score"], mover)
        alternatives.append(
            expected_points_from_cp(alternative_score)
        )

    if not alternatives:
        return False

    best_alternative = max(alternatives)

    return after_expected - best_alternative >= 0.10


def detect_miss(
    best_expected,
    played_expected,
    before_expected
):
    """
    Heuristic Miss detection.

    Looks for a missed opportunity to reach a clearly winning
    expected-points position.
    """

    if best_expected >= 0.85 and played_expected < 0.75:
        return True

    # Also detect a large missed opportunity from an equal position.
    if (
        0.35 <= before_expected <= 0.65
        and best_expected >= 0.90
        and played_expected < 0.80
    ):
        return True

    return False


# ============================================================
# CHESSBOARD WIDGET
# ============================================================

class ChessBoard(tk.Canvas):

    def __init__(self, parent, size=480):
        super().__init__(
            parent,
            width=size,
            height=size,
            bg="#202020",
            highlightthickness=0
        )

        self.size = size
        self.square = size / 8
        self.board = chess.Board()
        self.move = None
        self.best_move = None
        self.animations = []
        self.animation_step = 0
        self.animation_frames = 14
        self.animation_job = None

        self.draw()

    def set_position(self, fen, move=None, best_move=None, animation_move=None, reverse=False):
        if self.animation_job is not None:
            try:
                self.after_cancel(self.animation_job)
            except tk.TclError:
                pass
            self.animation_job = None
        old_board = self.board
        target_board = chess.Board(fen)
        self.move = move
        self.best_move = best_move
        self.animations = []

        animation_move = animation_move or move
        if animation_move:
            moving_from = animation_move.to_square if reverse else animation_move.from_square
            moving_to = animation_move.from_square if reverse else animation_move.to_square
            moving_piece = old_board.piece_at(moving_from)
            if moving_piece:
                self.animations.append((moving_piece, moving_from, moving_to))

            # Animate the rook in either direction when castling.
            if (
                moving_piece
                and moving_piece.piece_type == chess.KING
                and abs(
                    chess.square_file(animation_move.to_square)
                    - chess.square_file(animation_move.from_square)
                ) == 2
            ):
                rank = chess.square_rank(animation_move.from_square)
                kingside = chess.square_file(animation_move.to_square) > chess.square_file(animation_move.from_square)
                if reverse:
                    rook_from = chess.square(5 if kingside else 3, rank)
                    rook_to = chess.square(7 if kingside else 0, rank)
                else:
                    rook_from = chess.square(7 if kingside else 0, rank)
                    rook_to = chess.square(5 if kingside else 3, rank)
                rook_piece = old_board.piece_at(rook_from)
                if rook_piece:
                    self.animations.append((rook_piece, rook_from, rook_to))

        self.board = target_board
        self.animation_step = 0
        self.animation_frames = 14
        if self.animations:
            self.draw()
            self.animate_step()
        else:
            self.draw()

    def animate_step(self):
        self.animation_step += 1
        self.draw()
        if self.animation_step < self.animation_frames:
            self.animation_job = self.after(16, self.animate_step)
        else:
            self.animations = []
            self.draw()

    def draw(self):
        self.delete("all")
        sq = self.square
        light = "#f0d9b5"
        dark = "#b58863"
        animated_destinations = {item[2] for item in self.animations}

        for row in range(8):
            for col in range(8):
                x1 = col * sq
                y1 = row * sq
                x2 = x1 + sq
                y2 = y1 + sq
                square = chess.square(col, 7 - row)
                color = light if (row + col) % 2 == 0 else dark

                if self.move:
                    if square == self.move.from_square:
                        color = "#f6e65c"
                    elif square == self.move.to_square:
                        color = "#e5c842"

                self.create_rectangle(x1, y1, x2, y2, fill=color, outline="")
                piece = self.board.piece_at(square)

                if piece and square not in animated_destinations:
                    self.create_text(
                        x1 + sq / 2, y1 + sq / 2,
                        text=PIECES[piece.symbol()],
                        font=("Segoe UI Symbol", int(sq * 0.72)),
                        fill="#fffdf4" if piece.color else "#171717"
                    )

                if col == 0:
                    self.create_text(
                        x1 + 5, y1 + 5, anchor="nw",
                        text=str(8 - row),
                        font=("Arial", 9, "bold"), fill="#333333"
                    )
                if row == 7:
                    self.create_text(
                        x2 - 5, y2 - 5, anchor="se",
                        text=chess.FILE_NAMES[col],
                        font=("Arial", 9, "bold"), fill="#333333"
                    )

        # Draw moving pieces between their original and destination squares.
        if self.animations:
            progress = min(1.0, self.animation_step / self.animation_frames)
            # Smooth ease-in/ease-out interpolation.
            progress = progress * progress * (3 - 2 * progress)
            for piece, start, end in self.animations:
                sx = (chess.square_file(start) + 0.5) * sq
                sy = (7 - chess.square_rank(start) + 0.5) * sq
                ex = (chess.square_file(end) + 0.5) * sq
                ey = (7 - chess.square_rank(end) + 0.5) * sq
                self.create_text(
                    sx + (ex - sx) * progress,
                    sy + (ey - sy) * progress,
                    text=PIECES[piece.symbol()],
                    font=("Segoe UI Symbol", int(sq * 0.72)),
                    fill="#fffdf4" if piece.color else "#171717"
                )

        # Blue arrow = Stockfish's recommended move.
        if self.best_move:
            start = self.best_move.from_square
            end = self.best_move.to_square
            x1 = (chess.square_file(start) + 0.5) * sq
            y1 = (7 - chess.square_rank(start) + 0.5) * sq
            x2 = (chess.square_file(end) + 0.5) * sq
            y2 = (7 - chess.square_rank(end) + 0.5) * sq
            self.create_line(
                x1, y1, x2, y2,
                fill="#1688ff", width=6, arrow=tk.LAST,
                arrowshape=(16, 20, 7)
            )


# ============================================================
# MAIN APPLICATION
# ============================================================

class PGNReviewer:

    def __init__(self, root):
        self.root = root

        self.root.title("PGN Chess Reviewer")
        self.root.geometry("1500x920")
        self.root.minsize(1100, 720)

        # A cleaner, dark analysis-room look inspired by Chess.com.
        self.style = ttk.Style()
        try:
            self.style.theme_use("clam")
        except tk.TclError:
            pass
        self.style.configure(
            "Treeview",
            font=("Segoe UI", 10),
            rowheight=32,
            background="#202b36",
            fieldbackground="#202b36",
            foreground="#e8edf2",
            borderwidth=0
        )
        self.style.configure(
            "Treeview.Heading",
            font=("Segoe UI", 10, "bold"),
            background="#17212b",
            foreground="#e8edf2",
            padding=(8, 9)
        )
        self.style.map(
            "Treeview",
            background=[("selected", "#315b83")],
            foreground=[("selected", "#ffffff")]
        )
        self.style.configure(
            "MoveClass.TLabel",
            font=("Segoe UI", 15, "bold"),
            padding=(10, 7),
            background="#263442",
            foreground="#ffffff"
        )

        self.engine_path = find_engine()
        self.busy = False
        self.rows = []
        self.last_report = None
        self.current_selected_index = None

        self.build_ui()

    # --------------------------------------------------------
    # USER INTERFACE
    # --------------------------------------------------------

    def build_ui(self):

        outer = ttk.Frame(self.root, padding=12)
        outer.pack(fill="both", expand=True)

        ttk.Label(
            outer,
            text="PGN Chess Reviewer",
            font=("Segoe UI", 21, "bold")
        ).pack(anchor="w")

        toolbar = ttk.Frame(outer)
        toolbar.pack(fill="x", pady=10)

        ttk.Button(
            toolbar,
            text="Choose Stockfish",
            command=self.choose_engine
        ).pack(side="left")

        ttk.Label(
            toolbar,
            text="Depth:"
        ).pack(side="left", padx=(15, 4))

        self.depth = tk.IntVar(value=DEFAULT_DEPTH)

        ttk.Spinbox(
            toolbar,
            from_=8,
            to=30,
            width=5,
            textvariable=self.depth
        ).pack(side="left")

        ttk.Button(
            toolbar,
            text="Load PGN",
            command=self.load_pgn
        ).pack(side="right")

        self.engine_label = ttk.Label(
            outer,
            text="Stockfish: " + (
                self.engine_path if self.engine_path
                else "Not selected"
            )
        )

        self.engine_label.pack(anchor="w")

        ttk.Label(
            outer,
            text="Paste your PGN:",
            font=("Segoe UI", 10, "bold")
        ).pack(anchor="w", pady=(8, 3))

        self.pgn_text = tk.Text(
            outer,
            height=7,
            font=("Consolas", 10),
            wrap="word"
        )

        self.pgn_text.pack(fill="x")

        actions = ttk.Frame(outer)
        actions.pack(fill="x", pady=8)

        self.review_button = ttk.Button(
            actions,
            text="Review Game",
            command=self.start_review
        )

        self.review_button.pack(side="left")

        ttk.Button(
            actions,
            text="Clear",
            command=lambda: self.pgn_text.delete("1.0", "end")
        ).pack(side="left", padx=5)

        ttk.Button(
            actions,
            text="Save Report",
            command=self.save_report
        ).pack(side="right")

        self.progress = ttk.Progressbar(
            outer,
            mode="determinate"
        )

        self.progress.pack(fill="x", pady=5)

        self.status = tk.StringVar(
            value="Paste a PGN and click Review Game."
        )

        ttk.Label(
            outer,
            textvariable=self.status
        ).pack(anchor="w")

        main = ttk.PanedWindow(
            outer,
            orient="horizontal"
        )

        main.pack(fill="both", expand=True, pady=10)

        board_frame = ttk.Frame(main, padding=5)
        moves_frame = ttk.Frame(main, padding=5)

        main.add(board_frame, weight=1)
        main.add(moves_frame, weight=2)

        ttk.Label(
            board_frame,
            text="Position",
            font=("Segoe UI", 13, "bold")
        ).pack(anchor="w", pady=5)

        board_display = ttk.Frame(board_frame)
        board_display.pack(anchor="center")

        self.eval_bar = tk.Canvas(
            board_display, width=34, height=480,
            bg="#202020", highlightthickness=0
        )
        self.eval_bar.pack(side="left", padx=(0, 8))
        self.eval_white = 0.5
        self.eval_text = tk.StringVar(value="0.0")
        self.board = ChessBoard(board_display, 480)
        self.board.pack(side="left")
        self.draw_eval_bar(0.5, "0.0")

        navigation = ttk.Frame(board_frame)
        navigation.pack(fill="x", pady=(10, 3))
        self.previous_button = ttk.Button(
            navigation, text="◀ Previous", command=self.previous_move
        )
        self.previous_button.pack(side="left", expand=True, fill="x", padx=(0, 4))
        self.next_button = ttk.Button(
            navigation, text="Next ▶", command=self.next_move
        )
        self.next_button.pack(side="left", expand=True, fill="x", padx=(4, 0))

        self.position_label = tk.StringVar(
            value="Select a move to see its position."
        )

        ttk.Label(
            board_frame,
            textvariable=self.position_label,
            wraplength=460
        ).pack(anchor="w", pady=(8, 3))

        self.selected_move_title = tk.StringVar(
            value="Select a move from the review"
        )
        ttk.Label(
            board_frame,
            textvariable=self.selected_move_title,
            font=("Segoe UI", 17, "bold")
        ).pack(anchor="w", pady=(2, 5))

        self.selected_classification = tk.StringVar(value="")

        self.classification_badge = ttk.Label(
            board_frame,
            textvariable=self.selected_classification,
            style="MoveClass.TLabel"
        )
        self.classification_badge.pack(anchor="w", pady=(0, 8))

        self.recommendation = tk.StringVar(value="")

        ttk.Label(
            board_frame,
            textvariable=self.recommendation,
            wraplength=460,
            font=("Segoe UI", 10, "bold")
        ).pack(anchor="w")

        ttk.Label(
            moves_frame,
            text="Move-by-move review",
            font=("Segoe UI", 13, "bold")
        ).pack(anchor="w", pady=5)

        columns = (
            "move",
            "side",
            "played",
            "classification",
            "eval",
            "loss",
            "best"
        )

        self.results = ttk.Treeview(
            moves_frame,
            columns=columns,
            show="headings",
            height=22,
            style="Treeview"
        )

        headings = {
            "move": "Move",
            "side": "Player",
            "played": "Played",
            "classification": "Review",
            "eval": "Eval",
            "loss": "EP lost",
            "best": "Best move"
        }

        widths = {
            "move": 82,
            "side": 75,
            "played": 85,
            "classification": 125,
            "eval": 75,
            "loss": 78,
            "best": 90
        }

        for col in columns:
            self.results.heading(
                col,
                text=headings[col]
            )

            self.results.column(
                col,
                width=widths[col],
                anchor="center"
            )

        scrollbar = ttk.Scrollbar(
            moves_frame,
            orient="vertical",
            command=self.results.yview
        )

        self.results.configure(
            yscrollcommand=scrollbar.set
        )

        self.results.pack(
            side="left",
            fill="both",
            expand=True
        )

        scrollbar.pack(
            side="right",
            fill="y"
        )

        # Soft colored rows make important classifications visible at a glance.
        # Dark tinted row backgrounds with the corresponding Chess.com-style
        # classification color as the text/accent.
        row_colors = {
            "BRILLIANT": ("#173B39", COLORS["BRILLIANT"]),
            "GREAT": ("#1D354B", COLORS["GREAT"]),
            "BEST": ("#293A24", COLORS["BEST"]),
            "EXCELLENT": ("#354321", COLORS["EXCELLENT"]),
            "GOOD": ("#394332", COLORS["GOOD"]),
            "INACCURACY": ("#493D22", COLORS["INACCURACY"]),
            "MISTAKE": ("#4B3420", COLORS["MISTAKE"]),
            "BLUNDER": ("#492326", COLORS["BLUNDER"]),
            "MISS": ("#432B49", COLORS["MISS"]),
        }
        for label, (background, foreground) in row_colors.items():
            self.results.tag_configure(
                label,
                background=background,
                foreground=foreground
            )

        self.results.bind(
            "<<TreeviewSelect>>",
            self.select_move
        )

        self.summary = tk.StringVar(value="")
        self.performance_summary = tk.StringVar(value="")

        # Keep these results above the expanding board/table pane so they
        # remain visible instead of being pushed below the window.
        summary_panel = ttk.Frame(outer, padding=(4, 5))
        summary_panel.pack(fill="x", before=main)

        ttk.Label(
            summary_panel,
            textvariable=self.summary,
            font=("Segoe UI", 10, "bold"),
            wraplength=1350
        ).pack(anchor="w")

        ttk.Label(
            summary_panel,
            textvariable=self.performance_summary,
            font=("Segoe UI", 11, "bold"),
            foreground="#1BAAA0",
            wraplength=1350
        ).pack(anchor="w", pady=(3, 0))

        ttk.Label(
            summary_panel,
            text=(
                "Game-strength estimates use Stockfish move quality and are "
                "approximate, not Chess.com's proprietary formula."
            ),
            foreground="#777777"
        ).pack(anchor="w", pady=4)

    def draw_eval_bar(self, white_expected, score_text):
        """Animate the evaluation marker smoothly to the new position."""
        target = max(0.0, min(1.0, float(white_expected)))

        # Cancel any previous animation, but preserve the currently displayed
        # value so a quick sequence of selections never causes a jump.
        if getattr(self, "eval_animation_job", None) is not None:
            try:
                self.root.after_cancel(self.eval_animation_job)
            except tk.TclError:
                pass
            self.eval_animation_job = None

        start = getattr(self, "eval_white", 0.5)
        self.eval_text.set(score_text)
        self.eval_animation_start = start
        self.eval_animation_target = target
        self.eval_animation_step = 0
        self.eval_animation_frames = 30

        # Avoid animating the initial centered bar when the interface opens.
        if not hasattr(self, "eval_bar_initialized"):
            self.eval_bar_initialized = True
            self.eval_white = target
            self._render_eval_bar(target)
            return

        if abs(target - start) < 0.002:
            self.eval_white = target
            self._render_eval_bar(target)
            return

        self._animate_eval_bar()

    def _animate_eval_bar(self):
        self.eval_animation_step += 1
        progress = min(1.0, self.eval_animation_step / self.eval_animation_frames)

        # Smooth ease-in/ease-out over approximately half a second.
        eased = progress * progress * (3.0 - 2.0 * progress)
        value = (
            self.eval_animation_start
            + (self.eval_animation_target - self.eval_animation_start) * eased
        )

        # Keep the live value so an interrupted animation resumes from here.
        self.eval_white = value
        self._render_eval_bar(value)

        if self.eval_animation_step < self.eval_animation_frames:
            self.eval_animation_job = self.root.after(16, self._animate_eval_bar)
        else:
            self.eval_animation_job = None
            self.eval_white = self.eval_animation_target
            self._render_eval_bar(self.eval_white)

    def _render_eval_bar(self, white_expected):
        """Render one frame of the vertical evaluation bar."""
        self.eval_bar.delete("all")
        height = 480
        white_expected = max(0.0, min(1.0, white_expected))
        black_height = height * (1.0 - white_expected)

        self.eval_bar.create_rectangle(
            0, 0, 34, black_height, fill="#30343b", outline=""
        )
        self.eval_bar.create_rectangle(
            0, black_height, 34, height, fill="#f4f4f4", outline=""
        )
        self.eval_bar.create_text(
            17, 12, text="B", fill="#ffffff",
            font=("Segoe UI", 9, "bold")
        )
        self.eval_bar.create_text(
            17, height - 12, text="W", fill="#222222",
            font=("Segoe UI", 9, "bold")
        )
        self.eval_bar.create_rectangle(
            0, black_height - 1, 34, black_height + 1,
            fill="#d6b15e", outline=""
        )

    def previous_move(self):
        selection = self.results.selection()
        if not selection:
            return
        index = int(selection[0])
        if index > 0:
            target = str(index - 1)
            self.results.selection_set(target)
            self.results.focus(target)
            self.results.see(target)
            self.select_move()

    def next_move(self):
        selection = self.results.selection()
        if not selection:
            return
        index = int(selection[0])
        if index + 1 < len(self.rows):
            target = str(index + 1)
            self.results.selection_set(target)
            self.results.focus(target)
            self.results.see(target)
            self.select_move()

    # --------------------------------------------------------
    # FILE AND ENGINE CONTROLS
    # --------------------------------------------------------

    def choose_engine(self):

        path = filedialog.askopenfilename(
            title="Select Stockfish",
            filetypes=[
                ("Stockfish executable", "*.exe"),
                ("All files", "*.*")
            ]
        )

        if path:
            self.engine_path = path

            self.engine_label.config(
                text="Stockfish: " + path
            )

    def load_pgn(self):

        path = filedialog.askopenfilename(
            filetypes=[("PGN files", "*.pgn")]
        )

        if not path:
            return

        try:
            with open(path, "r", encoding="utf-8-sig") as f:
                self.pgn_text.delete("1.0", "end")
                self.pgn_text.insert("1.0", f.read())

        except Exception as e:
            messagebox.showerror("Error", str(e))

    # --------------------------------------------------------
    # START REVIEW
    # --------------------------------------------------------

    def start_review(self):

        if self.busy:
            return

        pgn_data = self.pgn_text.get(
            "1.0",
            "end"
        ).strip()

        if not pgn_data:
            messagebox.showwarning(
                "Missing PGN",
                "Paste a PGN first."
            )
            return

        if (
            not self.engine_path
            or not os.path.isfile(self.engine_path)
        ):
            messagebox.showwarning(
                "Stockfish missing",
                "Download Stockfish and select stockfish.exe."
            )
            return

        try:
            depth = int(self.depth.get())

            if not 8 <= depth <= 30:
                raise ValueError

        except ValueError:
            messagebox.showerror(
                "Invalid depth",
                "Use a depth from 8 to 30."
            )
            return

        self.busy = True
        self.review_button.config(state="disabled")

        self.results.delete(
            *self.results.get_children()
        )

        self.summary.set("")
        self.performance_summary.set("")
        self.progress["value"] = 0

        self.rows = []
        self.last_report = None
        self.current_selected_index = None

        threading.Thread(
            target=self.review_worker,
            args=(pgn_data, depth),
            daemon=True
        ).start()

    # --------------------------------------------------------
    # ANALYSIS WORKER
    # --------------------------------------------------------

    def review_worker(self, pgn_data, depth):

        engine = None

        try:
            stream = io.StringIO(pgn_data)
            games = []

            while True:
                game = chess.pgn.read_game(stream)

                if game is None:
                    break

                if game.errors:
                    raise ValueError(
                        "PGN errors: "
                        + "; ".join(map(str, game.errors))
                    )

                games.append(game)

            if not games:
                raise ValueError(
                    "No valid games found in the PGN."
                )

            engine = chess.engine.SimpleEngine.popen_uci(
                self.engine_path
            )

            rows = []
            counts = {}

            total = sum(
                len(list(game.mainline_moves()))
                for game in games
            )

            completed = 0

            for game_number, game in enumerate(games, 1):

                board = game.board()
                moves = list(game.mainline_moves())

                white_rating = get_rating(
                    game,
                    "WhiteElo"
                )

                black_rating = get_rating(
                    game,
                    "BlackElo"
                )

                for ply, move in enumerate(moves):

                    color = board.turn
                    move_number = board.fullmove_number

                    player = (
                        "White" if color else "Black"
                    )

                    player_rating = (
                        white_rating if color
                        else black_rating
                    )

                    san = board.san(move)
                    fen_before = board.fen()

                    # Position before the move.
                    before_info = engine.analyse(
                        board,
                        chess.engine.Limit(depth=depth)
                    )

                    before_score = score_cp(
                        before_info["score"],
                        color
                    )

                    best_move = before_info.get(
                        "pv",
                        [None]
                    )[0]

                    forced = (
                        board.legal_moves.count() == 1
                    )

                    # Position after the played move.
                    after = board.copy()
                    after.push(move)

                    after_info = engine.analyse(
                        after,
                        chess.engine.Limit(depth=depth)
                    )

                    after_score = score_cp(
                        after_info["score"],
                        color
                    )

                    # Expected points calculation.
                    before_expected = (
                        expected_points_from_cp(
                            before_score
                        )
                    )

                    after_expected = (
                        expected_points_from_cp(
                            after_score
                        )
                    )

                    expected_points_lost = max(
                        0.0,
                        before_expected - after_expected
                    )

                    # Evaluate the best alternative position.
                    best_expected = after_expected
                    best_score = after_score

                    if best_move:
                        best_board = board.copy()
                        best_board.push(best_move)

                        best_info = engine.analyse(
                            best_board,
                            chess.engine.Limit(depth=depth)
                        )

                        best_score = score_cp(
                            best_info["score"],
                            color
                        )

                        best_expected = (
                            expected_points_from_cp(
                                best_score
                            )
                        )

                    # Special classifications.
                    brilliant = False
                    great = False
                    miss = False

                    if not forced:

                        brilliant = detect_brilliant(
                            engine,
                            board,
                            after,
                            move,
                            best_move,
                            before_score,
                            after_score,
                            color,
                            depth
                        )

                        if not brilliant:
                            great = detect_great(
                                engine,
                                board,
                                move,
                                best_move,
                                before_expected,
                                after_expected,
                                color,
                                depth
                            )

                        if not brilliant and not great:
                            miss = detect_miss(
                                best_expected,
                                after_expected,
                                before_expected
                            )

                    label = classify_move(
                        expected_points_lost,
                        brilliant=brilliant,
                        great=great,
                        miss=miss
                    )

                    # SAN for the best move.
                    best_san = "-"

                    if (
                        best_move
                        and best_move in board.legal_moves
                    ):
                        best_san = board.san(best_move)

                    # Evaluation display.
                    if abs(after_score) >= 100000:
                        eval_text = "Mate"
                    else:
                        eval_text = (
                            f"{after_score / 100:+.2f}"
                        )

                    loss_text = (
                        f"{expected_points_lost:.3f}"
                    )

                    rows.append({
                        "move_number": move_number,
                        "player": player,
                        "rating": player_rating,
                        "san": san,
                        "label": label,
                        "eval": eval_text,
                        "loss": loss_text,
                        "game": game_number,
                        "fen": fen_before,
                        "move_uci": move.uci(),
                        "best_uci": (
                            best_move.uci()
                            if best_move else None
                        ),
                        "best_san": best_san,
                        "before_expected": before_expected,
                        "after_expected": after_expected,
                        "white_expected": (
                            after_expected if color == chess.WHITE
                            else 1.0 - after_expected
                        ),
                        "best_expected": best_expected,
                        "quality_loss": max(0.0, best_expected - after_expected),
                        "forced": forced,
                    })

                    counts[label] = (
                        counts.get(label, 0) + 1
                    )

                    board.push(move)
                    completed += 1

                    self.root.after(
                        0,
                        self.update_progress,
                        completed,
                        total,
                        game_number
                    )

            engine.quit()
            engine = None

            (
                white_performance,
                white_games,
                black_performance,
                black_games,
                white_loss,
                black_loss
            ) = calculate_game_strengths(rows)

            self.root.after(
                0,
                self.show_results,
                rows,
                counts,
                len(games),
                white_performance,
                white_games,
                black_performance,
                black_games,
                white_loss,
                black_loss
            )

        except Exception as e:

            if engine:
                try:
                    engine.quit()
                except Exception:
                    pass

            self.root.after(
                0,
                self.review_failed,
                str(e)
            )

    # --------------------------------------------------------
    # PROGRESS
    # --------------------------------------------------------

    def update_progress(
        self,
        completed,
        total,
        game_number
    ):

        self.progress["maximum"] = max(total, 1)
        self.progress["value"] = completed

        self.status.set(
            f"Analyzing {completed}/{total} moves "
            f"| Game {game_number}"
        )

    # --------------------------------------------------------
    # DISPLAY RESULTS
    # --------------------------------------------------------

    def show_results(
        self,
        rows,
        counts,
        game_count,
        white_performance=None,
        white_games=0,
        black_performance=None,
        black_games=0,
        white_loss=None,
        black_loss=None
    ):

        self.rows = rows

        for index, row in enumerate(rows):

            move_label = (
                f"G{row['game']} {row['move_number']}"
            )

            if row["player"] == "Black":
                move_label += "..."

            self.results.insert(
                "",
                "end",
                iid=str(index),
                values=(
                    move_label,
                    row["player"],
                    row["san"],
                    row["label"],
                    row["eval"],
                    row["loss"],
                    row["best_san"]
                ),
                tags=(row["label"],)
            )

        order = [
            "BRILLIANT",
            "GREAT",
            "BEST",
            "EXCELLENT",
            "GOOD",
            "INACCURACY",
            "MISTAKE",
            "BLUNDER",
            "MISS"
        ]

        summary = " | ".join(
            f"{name}: {counts.get(name, 0)}"
            for name in order
            if counts.get(name, 0)
        )

        self.summary.set(
            f"{len(rows)} moves reviewed across "
            f"{game_count} game(s). "
            + summary
        )

        white_text = (
            f"White: ~{white_performance} Elo"
            if white_performance is not None else "White: not enough moves"
        )
        black_text = (
            f"Black: ~{black_performance} Elo"
            if black_performance is not None else "Black: not enough moves"
        )
        white_detail = f" (avg EP loss {white_loss:.3f})" if white_loss is not None else ""
        black_detail = f" (avg EP loss {black_loss:.3f})" if black_loss is not None else ""
        self.performance_summary.set(
            f"Estimated performance (experimental) — {white_text}{white_detail}  |  "
            f"{black_text}{black_detail}"
        )

        self.status.set("Review complete.")

        self.review_button.config(state="normal")
        self.busy = False

        self.last_report = (
            rows,
            counts,
            game_count,
            white_performance,
            white_games,
            black_performance,
            black_games,
            white_loss,
            black_loss
        )

        if rows:
            self.results.selection_set("0")
            self.results.focus("0")
            self.select_move()

    # --------------------------------------------------------
    # SELECT MOVE
    # --------------------------------------------------------

    def select_move(self, event=None):

        selection = self.results.selection()

        if not selection:
            return

        index = int(selection[0])

        if index >= len(self.rows):
            return

        row = self.rows[index]

        # Put the exact move and its classification in a large, obvious
        # summary so users do not have to infer which row the label belongs to.
        move_number_text = (
            f"{row['move_number']}."
            if row["player"] == "White"
            else f"{row['move_number']}..."
        )
        self.selected_move_title.set(
            f"{move_number_text} {row['san']}  ·  {row['player']}"
        )
        self.selected_classification.set(row["label"])

        class_foregrounds = COLORS
        self.style.configure(
            "MoveClass.TLabel",
            foreground=class_foregrounds.get(row["label"], "#ffffff")
        )

        move = chess.Move.from_uci(row["move_uci"])
        position_after = chess.Board(row["fen"])
        position_after.push(move)

        # When stepping to the previous row, animate the move being undone.
        reverse = (
            self.current_selected_index is not None
            and index == self.current_selected_index - 1
            and self.rows[self.current_selected_index]["game"] == row["game"]
        )
        animation_move = (
            chess.Move.from_uci(self.rows[self.current_selected_index]["move_uci"])
            if reverse else move
        )
        self.board.set_position(
            position_after.fen(),
            move,
            chess.Move.from_uci(row["best_uci"])
            if row["best_uci"] else None,
            animation_move=animation_move,
            reverse=reverse
        )
        self.current_selected_index = index
        self.draw_eval_bar(
            row.get("white_expected", 0.5),
            row["eval"]
        )

        self.position_label.set(
            f"Game {row['game']} | "
            f"{row['player']} | "
            f"Move {row['move_number']}"
        )

        self.recommendation.set(
            f"Played: {row['san']}\n"
            f"Stockfish best: {row['best_san']}\n"
            f"Classification: {row['label']}\n"
            f"Player rating: {row['rating']}\n"
            f"Expected points before: "
            f"{row['before_expected']:.3f}\n"
            f"Expected points after: "
            f"{row['after_expected']:.3f}\n"
            f"Expected points lost: "
            f"{row['loss']}\n"
            f"Best move expected points: "
            f"{row['best_expected']:.3f}\n\n"
            f"Blue arrow = Stockfish's recommended move."
        )

    # --------------------------------------------------------
    # ERROR HANDLING
    # --------------------------------------------------------

    def review_failed(self, error):

        self.busy = False

        self.review_button.config(
            state="normal"
        )

        self.status.set("Review failed.")

        messagebox.showerror(
            "Review failed",
            error
        )

    # --------------------------------------------------------
    # SAVE REPORT
    # --------------------------------------------------------

    def save_report(self):

        if not self.last_report:
            messagebox.showinfo(
                "No report",
                "Review a game first."
            )
            return

        path = filedialog.asksaveasfilename(
            defaultextension=".txt",
            filetypes=[
                ("Text report", "*.txt")
            ]
        )

        if not path:
            return

        (
            rows,
            counts,
            game_count,
            white_performance,
            white_games,
            black_performance,
            black_games,
            white_loss,
            black_loss
        ) = self.last_report

        with open(
            path,
            "w",
            encoding="utf-8"
        ) as f:

            f.write(
                f"PGN Chess Review - {game_count} games\n"
            )

            f.write("=" * 80 + "\n\n")

            f.write(
                "Classification method: "
                "Chess.com-style expected-points approximation\n"
            )

            f.write(
                f"Estimated performance (experimental) — White: {white_performance} Elo; "
                f"Black: {black_performance} Elo.\n"
                "Uses both PGN starting ratings and relative Stockfish move quality. "
                "It is not Chess.com's proprietary Game Rating formula.\n\n"
            )

            for row in rows:

                f.write(
                    f"Game {row['game']} | "
                    f"Move {row['move_number']} | "
                    f"{row['player']} | "
                    f"Rating: {row['rating']} | "
                    f"{row['san']} | "
                    f"{row['label']} | "
                    f"Eval: {row['eval']} | "
                    f"EP lost: {row['loss']} | "
                    f"Best: {row['best_san']}\n"
                )

            f.write("\nSUMMARY\n")
            f.write("=" * 40 + "\n")

            for label, count in sorted(counts.items()):
                f.write(
                    f"{label}: {count}\n"
                )

        messagebox.showinfo(
            "Saved",
            "Report saved successfully."
        )


# ============================================================
# START APPLICATION
# ============================================================

if __name__ == "__main__":

    root = tk.Tk()

    app = PGNReviewer(root)

    root.mainloop()
