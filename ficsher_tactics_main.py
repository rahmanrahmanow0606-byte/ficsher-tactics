import io, os, math, threading
from functools import partial
import chess, chess.pgn, chess.engine
from kivy.app import App
from kivy.clock import Clock
from kivy.core.clipboard import Clipboard
from kivy.metrics import dp
from kivy.utils import get_color_from_hex
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.button import Button
from kivy.uix.label import Label
from kivy.uix.textinput import TextInput
from kivy.uix.scrollview import ScrollView
from kivy.uix.progressbar import ProgressBar

APP_NAME = 'Ficsher Tactics'
DEFAULT_RATING = 1200
PALETTE = {'BRILLIANT':'#1BAAA0','GREAT':'#5B9BD5','BEST':'#5C8A3C','EXCELLENT':'#96BC4B','GOOD':'#A8C47A','INACCURACY':'#F0C15A','MISTAKE':'#E58C2A','BLUNDER':'#C03030','MISS':'#C77BD4'}

def expected_points_from_cp(cp):
    if cp >= 100000: return 1.0
    if cp <= -100000: return 0.0
    return 1.0 / (1.0 + math.exp(-max(-10000, min(10000, cp)) / 400.0))

def score_cp(score, color):
    score = score.pov(color)
    if score.is_mate(): return 100000 if (score.mate() or 0) > 0 else -100000
    return score.score()

def classify_move(loss, brilliant=False, great=False, miss=False):
    if brilliant: return 'BRILLIANT'
    if great: return 'GREAT'
    if miss: return 'MISS'
    loss=max(0.0,loss)
    if loss < .005: return 'BEST'
    if loss < .02: return 'EXCELLENT'
    if loss < .05: return 'GOOD'
    if loss < .10: return 'INACCURACY'
    if loss < .20: return 'MISTAKE'
    return 'BLUNDER'

def get_rating(game, header):
    try:
        r=int(game.headers.get(header, DEFAULT_RATING))
        return r if 100 <= r <= 4000 else DEFAULT_RATING
    except (ValueError, TypeError): return DEFAULT_RATING

def material(board, color):
    vals={chess.PAWN:1,chess.KNIGHT:3,chess.BISHOP:3,chess.ROOK:5,chess.QUEEN:9,chess.KING:0}
    return sum(len(board.pieces(pt,color))*v for pt,v in vals.items())

def detect_brilliant(engine, before, after, move, best, before_score, after_score, mover, depth):
    if best is None or best != move or before.legal_moves.count() <= 1: return False
    piece=before.piece_at(move.from_square)
    if piece is None or piece.piece_type not in (chess.KNIGHT,chess.BISHOP,chess.ROOK,chess.QUEEN): return False
    before_ep=expected_points_from_cp(before_score); after_ep=expected_points_from_cp(after_score)
    if before_ep >= .90 or before_ep-after_ep > .015: return False
    try:
        info=engine.analyse(after,chess.engine.Limit(depth=max(10,depth))); pv=info.get('pv') or []
        if not pv: return False
        reply=pv[0]
        if reply.to_square != move.to_square or not after.is_capture(reply): return False
        captured=after.copy(); captured.push(reply)
        before_material=material(before,mover)-material(before,not mover)
        after_material=material(captured,mover)-material(captured,not mover)
        if after_material-before_material > -1.5: return False
        info2=engine.analyse(captured,chess.engine.Limit(depth=max(10,depth)))
        captured_ep=expected_points_from_cp(score_cp(info2['score'],mover))
        return captured_ep >= .58 and after_ep-captured_ep <= .12
    except (chess.engine.EngineError,chess.engine.EngineTerminatedError): return False

def detect_great(engine,before,move,best,before_ep,after_ep,mover,depth):
    if best != move or before.legal_moves.count() <= 1 or after_ep-before_ep < .20: return False
    alternatives=[]
    for alt in before.legal_moves:
        if alt == move: continue
        b=before.copy(); b.push(alt)
        info=engine.analyse(b,chess.engine.Limit(depth=max(8,depth-2)))
        alternatives.append(expected_points_from_cp(score_cp(info['score'],mover)))
    return bool(alternatives) and after_ep-max(alternatives) >= .10

def detect_miss(best_ep,played_ep,before_ep):
    return (best_ep >= .85 and played_ep < .75) or (0.35 <= before_ep <= .65 and best_ep >= .90 and played_ep < .80)

def performance_estimate(rows):
    stats={}
    for player in ('White','Black'):
        rs=[r for r in rows if r['player']==player and not r['forced']]
        if len(rs)<8: return None
        loss=sum(r['quality_loss'] for r in rs)/len(rs)
        ratings=[r['rating'] for r in rs if isinstance(r['rating'],int)]
        stats[player]=(loss, round(sum(ratings)/len(ratings)) if ratings else DEFAULT_RATING)
    w, b=stats['White'],stats['Black']
    wq=1/(1+10*w[0]); bq=1/(1+10*b[0])
    gap=max(-300,min(300,400*math.log10(max(.01,wq)/max(.01,bq))))
    baseline=(w[1]+b[1])/2
    return round(max(100,min(3000,baseline+gap/2))),round(max(100,min(3000,baseline-gap/2)))

def engine_path():
    try:
        from jnius import autoclass
        act=autoclass('org.kivy.android.PythonActivity').mActivity
        path=os.path.join(str(act.getApplicationInfo().nativeLibraryDir),'libstockfish.so')
        if os.path.isfile(path): return path
    except Exception: pass
    import shutil
    return shutil.which('stockfish') or ''

class FicsherApp(App):
    def build(self):
        self.title=APP_NAME; self.busy=False
        root=BoxLayout(orientation='vertical',padding=dp(12),spacing=dp(8))
        with root.canvas.before:
            from kivy.graphics import Color, Rectangle
            Color(*get_color_from_hex('#111827')); self.bg=Rectangle(pos=root.pos,size=root.size)
        root.bind(pos=lambda w,v:setattr(self.bg,'pos',v),size=lambda w,v:setattr(self.bg,'size',v))
        root.add_widget(Label(text='[b]Ficsher Tactics[/b]\nPGN Game Analyzer',markup=True,font_size='23sp',size_hint_y=None,height=dp(65),color=(.95,.96,.98,1)))
        self.pgn=TextInput(hint_text='Paste your PGN game here...',multiline=True,size_hint_y=.48,font_size='14sp',background_color=get_color_from_hex('#202938'),foreground_color=(1,1,1,1),padding=dp(10))
        root.add_widget(self.pgn)
        bar=BoxLayout(size_hint_y=None,height=dp(46),spacing=dp(7))
        self.depth=TextInput(text='12',hint_text='Depth 8-30',multiline=False,input_filter='int',size_hint_x=.25)
        bar.add_widget(self.depth)
        paste=Button(text='Paste PGN',size_hint_x=.34); paste.bind(on_release=self.paste); bar.add_widget(paste)
        self.go=Button(text='Analyze game',size_hint_x=.41,background_normal='',background_color=get_color_from_hex('#287A62')); self.go.bind(on_release=self.start); bar.add_widget(self.go)
        root.add_widget(bar)
        self.status=Label(text='Paste a PGN to begin.',size_hint_y=None,height=dp(48),font_size='13sp',color=(.8,.84,.9,1)); root.add_widget(self.status)
        self.progress=ProgressBar(max=100,value=0,size_hint_y=None,height=dp(8)); root.add_widget(self.progress)
        scroll=ScrollView(); self.results=BoxLayout(orientation='vertical',spacing=dp(5),size_hint_y=None,padding=[0,dp(4)]); self.results.bind(minimum_height=self.results.setter('height')); scroll.add_widget(self.results); root.add_widget(scroll)
        root.add_widget(Label(text='Move labels and performance rating are heuristic estimates.',size_hint_y=None,height=dp(22),font_size='10sp',color=(.55,.6,.67,1)))
        return root

    def paste(self,*_):
        try: self.pgn.text=Clipboard.paste(); self.status.text='PGN pasted. Press Analyze game.'
        except Exception as e: self.status.text='Clipboard error: '+str(e)

    def start(self,*_):
        if self.busy: return
        data=self.pgn.text.strip()
        if not data: self.status.text='Please paste a PGN first.'; return
        try:
            depth=int(self.depth.text)
            if not 8 <= depth <= 30: raise ValueError()
        except ValueError: self.status.text='Depth must be from 8 to 30.'; return
        path=engine_path()
        if not path: self.status.text='Stockfish engine not found in this APK.'; return
        self.busy=True; self.go.disabled=True; self.progress.value=0; self.results.clear_widgets(); self.status.text='Starting analysis...'
        threading.Thread(target=self.worker,args=(data,depth,path),daemon=True).start()

    def worker(self,data,depth,path):
        engine=None
        try:
            stream=io.StringIO(data); games=[]
            while True:
                game=chess.pgn.read_game(stream)
                if game is None: break
                if game.errors: raise ValueError('PGN errors: '+'; '.join(map(str,game.errors)))
                games.append(game)
            if not games: raise ValueError('No valid PGN game found.')
            engine=chess.engine.SimpleEngine.popen_uci(path)
            rows=[]; total=sum(len(list(g.mainline_moves())) for g in games); done=0
            for game_no,game in enumerate(games,1):
                board=game.board(); moves=list(game.mainline_moves())
                wr=get_rating(game,'WhiteElo'); br=get_rating(game,'BlackElo')
                for move in moves:
                    mover=board.turn; player='White' if mover else 'Black'; number=board.fullmove_number
                    rating=wr if mover else br; san=board.san(move)
                    before_info=engine.analyse(board,chess.engine.Limit(depth=depth)); before_score=score_cp(before_info['score'],mover); best=(before_info.get('pv') or [None])[0]
                    forced=board.legal_moves.count()==1
                    after=board.copy(); after.push(move)
                    after_info=engine.analyse(after,chess.engine.Limit(depth=depth)); after_score=score_cp(after_info['score'],mover)
                    before_ep=expected_points_from_cp(before_score); after_ep=expected_points_from_cp(after_score); loss=max(0,before_ep-after_ep)
                    best_ep=after_ep
                    if best:
                        bb=board.copy(); bb.push(best); bi=engine.analyse(bb,chess.engine.Limit(depth=depth)); best_ep=expected_points_from_cp(score_cp(bi['score'],mover))
                    brilliant=great=miss=False
                    if not forced:
                        brilliant=detect_brilliant(engine,board,after,move,best,before_score,after_score,mover,depth)
                        if not brilliant: great=detect_great(engine,board,move,best,before_ep,after_ep,mover,depth)
                        if not brilliant and not great: miss=detect_miss(best_ep,after_ep,before_ep)
                    label=classify_move(loss,brilliant,great,miss)
                    best_san=board.san(best) if best and best in board.legal_moves else '-'
                    eval_text='Mate' if abs(after_score)>=100000 else f'{after_score/100:+.2f}'
                    rows.append({'move_number':number,'player':player,'rating':rating,'san':san,'label':label,'eval':eval_text,'loss':loss,'best_san':best_san,'quality_loss':max(0,best_ep-after_ep),'forced':forced})
                    board.push(move); done+=1; pct=int(100*done/max(1,total)); Clock.schedule_once(partial(self.update_progress,pct,done,total))
            estimate=performance_estimate(rows); engine.quit(); engine=None
            Clock.schedule_once(partial(self.finish,rows,estimate))
        except Exception as e:
            if engine:
                try: engine.quit()
                except Exception: pass
            Clock.schedule_once(partial(self.fail,str(e)))

    def update_progress(self,pct,done,total,*_): self.progress.value=pct; self.status.text=f'Analyzing moves: {done}/{total} ({pct}%)'
    def fail(self,msg,*_): self.busy=False; self.go.disabled=False; self.status.text='Analysis failed: '+msg
    def add_card(self,text,color):
        from kivy.graphics import Color, RoundedRectangle
        w=Label(text=text,size_hint_y=None,height=dp(45 if len(text)<70 else 60),halign='left',valign='middle',text_size=(self.results.width-dp(24),None),padding=(dp(9),dp(4)),color=(1,1,1,1))
        with w.canvas.before:
            Color(*get_color_from_hex(color)); bg=RoundedRectangle(pos=w.pos,size=w.size,radius=[dp(7)])
        w.bind(pos=lambda obj,v:setattr(bg,'pos',v),size=lambda obj,v:setattr(bg,'size',v))
        self.results.add_widget(w)
    def finish(self,rows,estimate,*_):
        self.busy=False; self.go.disabled=False; counts={}
        for r in rows: counts[r['label']]=counts.get(r['label'],0)+1
        summary=f'Moves analyzed: {len(rows)}'
        if estimate: summary+=f'\nEstimated performance — White: {estimate[0]} | Black: {estimate[1]}'
        else: summary+='\nPerformance estimate unavailable (need at least 8 non-forced moves per player).'
        self.status.text=summary
        for name in PALETTE:
            if counts.get(name): self.add_card(f'{name}: {counts[name]}',PALETTE[name])
        for r in rows:
            self.add_card(f"{r['move_number']}. {r['player']}  {r['san']} — {r['label']} | Eval {r['eval']} | Best: {r['best_san']} | Loss: {r['loss']:.3f}",PALETTE[r['label']])
        self.progress.value=100

if __name__ == '__main__': FicsherApp().run()
