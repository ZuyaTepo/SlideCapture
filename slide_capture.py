"""
講義動画 → スライドPDF 化ツール

必要なライブラリ:
    pip install -r requirements.txt
    （PySide6, av, opencv-python-headless, pymupdf, numpy）

exe化:
    Windows ならフォルダ内の build.bat をダブルクリック（詳しくは README.md）

使い方:
    python slide_capture.py
    1. 「動画を開く」で動画を選ぶ（動画ファイルをウィンドウにドラッグ＆ドロップしてもよい）
    2. 「自動検出」でスライドの切り替わりを検出し、候補を一覧に追加する（任意）
    3. シークバーや矢印キーで場面を探し、「このフレームを追加」（Enter / Space）で手動追加
    4. 一覧はドラッグで並び替え、Deleteキーで削除、ダブルクリックでその時刻へ移動
       候補の場面がずれていたら、ダブルクリック → 矢印キーで調整 → R キーで置き換え
    5. 「PDF保存」で一覧の順にPDFへ書き出す（ページサイズ・解像度・画質を選べる）
    ※ 顔出し映像などで誤検出する場合は、検出前に「除外領域」を指定する

キー操作:
    ← / →           : 1秒戻る / 進む
    Shift + ← / →   : 10秒戻る / 進む
    , / .           : 1フレーム戻る / 進む
    Enter / Space   : 現在のフレームを追加
    Delete          : 一覧で選択中の項目を削除
    R               : 一覧で選択中の項目を、表示中のフレームで置き換え
    Esc             : 除外領域の描画モードを終了

除外領域:
    「除外領域を描く」をオンにしてプレビュー上をドラッグすると、その範囲を
    自動検出の対象から外せる（顔出し映像、スライド内の動画、字幕、時計など）。
    右クリックでその領域を削除、「除外領域をクリア」で全削除。
    領域は画面に対する割合で保存するので、解像度の違う動画でも使い回せる。
    除外領域は検出にだけ使い、PDFに保存する画像はそのまま。

自動検出のしくみ:
    一定間隔でフレームを縮小し、文字や図の輪郭（エッジ）を取り出す。
    前後のフレームでエッジがほとんど変わらなければ「安定」とみなす。
    安定した画面どうしを比べて、前の画面のエッジが「感度」以上の割合で
    消えていたらスライドの切り替わりとみなす。
    フレーム番号ではなく動画内の時刻で判定するので、Zoom の録画などの
    可変フレームレート（VFR）動画でも時間がずれない。
    「採用する場面」で、どの場面を候補にするかを選べる。
      ・次の切り替わりの直前：スライドの最後の状態（箇条書きが全部出そろった状態）
      ・切り替わりの直後　　：スライドが表示された最初の状態
    「余裕」の秒数だけ切り替わりの瞬間から離れた場面を選ぶので、
    候補の画像と、ダブルクリックで移動した先の画面が食い違いにくい。
    切り替え時のフェードやアニメーション中の画面は候補にならない。
"""

import math
import sys
from collections import deque
from dataclasses import dataclass
from pathlib import Path

import av  # PyAV（FFmpeg）：動画の読み込み
import cv2
import numpy as np
import pymupdf  # PyMuPDF
from PySide6.QtCore import (
    Qt, QTimer, QSize, QThread, Signal, QRectF, QPointF, QSettings, QEvent,
)
from PySide6.QtGui import (
    QImage, QPixmap, QIcon, QKeySequence, QShortcut, QPainter, QPen, QColor,
)
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QLabel, QSlider, QPushButton,
    QListWidget, QListWidgetItem, QFileDialog, QHBoxLayout, QVBoxLayout,
    QMessageBox, QAbstractItemView, QSizePolicy, QDoubleSpinBox, QProgressBar,
    QDialog, QDialogButtonBox, QComboBox, QSpinBox, QFormLayout, QProgressDialog,
)


def format_time(sec: float) -> str:
    sec = max(0, int(sec))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def bgr_to_qimage(frame) -> QImage:
    rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
    h, w, _ = rgb.shape
    return QImage(rgb.data, w, h, 3 * w, QImage.Format_RGB888).copy()


# ============================================================
# 動画の読み込み（GUIに依存しない部分）
# ============================================================
def _stream_info(container, stream):
    """(開始時刻, 長さ[秒], 平均fps) を返す"""
    tb = stream.time_base
    start = float(stream.start_time * tb) if stream.start_time is not None else 0.0
    fps = float(stream.average_rate or stream.guessed_rate or 30)
    if stream.duration:
        duration = float(stream.duration * tb)
    elif container.duration:
        duration = container.duration / av.time_base
    elif stream.frames:
        duration = stream.frames / fps
    else:
        duration = 0.0
    return start, duration, fps


class VideoReader:
    """時刻を指定して、その時刻に表示されているフレームを取り出す。
    フレームの時刻（タイムスタンプ）で位置を決めるので、Zoom の録画などの
    可変フレームレート（VFR）動画でも正確に移動できる。"""

    EPS = 0.0015   # シークバー（ミリ秒単位）の丸め誤差を吸収する

    def __init__(self, path):
        self.container = av.open(str(path))
        self.stream = self.container.streams.video[0]
        self.stream.thread_type = "AUTO"
        self.tb = self.stream.time_base
        self.start, self.duration, self.fps = _stream_info(self.container, self.stream)
        self._probe_end()
        self._gen = None
        self._last = None    # 最後に返したフレーム
        self._peek = None    # その次のフレーム（先読み）

    def close(self):
        self.container.close()

    def _probe_end(self):
        """ファイルに記録された長さが、最後のフレームより短い動画がある。
        末尾を実際に読んで、最後のフレームまでシークバーで行けるようにする。"""
        try:
            self.container.seek(int((self.start + self.duration + 10) / self.tb),
                                stream=self.stream, backward=True, any_frame=False)
            last = None
            for f in self.container.decode(self.stream):
                if f.time is not None:
                    last = f.time
            if last is not None:
                self.duration = max(self.duration, last - self.start + 1.0 / self.fps)
        except Exception:  # noqa: BLE001  長さの補正は、できなくても動作に支障はない
            pass

    def _next(self):
        if self._peek is not None:
            f, self._peek = self._peek, None
            return f
        for f in self._gen:
            if f.time is not None:
                return f
        return None

    def _seek(self, target):
        self.container.seek(max(0, int(target / self.tb)), stream=self.stream,
                            backward=True, any_frame=False)
        self._gen = self.container.decode(self.stream)
        self._last = self._peek = None

    def frame_at(self, t: float):
        """時刻 t[秒] に表示されているフレームを (そのフレームの時刻[秒], BGR画像) で返す"""
        target = t + self.start
        last = self._last
        # 少し先へ進むだけなら、シークせずに読み進める（矢印キー操作が速くなる）
        if not (last is not None and last.time <= target + self.EPS and target - last.time < 2.0):
            self._seek(target)
        while True:
            f = self._next()
            if f is None:
                break
            if f.time > target + self.EPS and self._last is not None:
                self._peek = f
                break
            self._last = f
        if self._last is None:
            return None
        return self._last.time - self.start, self._last.to_ndarray(format="bgr24")

    def next_time(self):
        """直前に返したフレームの、次のフレームの時刻[秒]（なければ None）"""
        if self._last is None:
            return None
        if self._peek is None:
            self._peek = self._next()
        return None if self._peek is None else self._peek.time - self.start


# ============================================================
# スライド切り替わり検出（GUIに依存しない部分）
# ============================================================
@dataclass
class DetectParams:
    change_ratio: float = 0.20   # 前のスライドの内容がこの割合以上消えたら別スライド（感度）
    interval_sec: float = 0.5    # 何秒ごとにフレームを調べるか
    stable_sec: float = 1.0      # この時間、画面が変化しなければ「安定」とみなす
    exclude: tuple = ()          # 除外領域 ((x0, y0, x1, y1), ...) 画面に対する割合 0〜1
    pick: str = "before"         # "before"：次の切り替わりの直前 / "after"：切り替わりの直後
    margin_sec: float = 1.0      # 切り替わりの瞬間から、この秒数だけ離れた場面を選ぶ


_KERNEL = np.ones((3, 3), np.uint8)
_MIN_EDGES = 300                 # これ未満のエッジしかない画面は「無地」とみなす
_CMP_W, _CMP_H = 1280, 720       # 比較に使うサイズ（低くすると同じ行の別の文字を同一視しやすい）


def make_exclude_mask(regions):
    """除外領域（割合）から比較サイズのマスクを作る。True が除外する画素。"""
    if not regions:
        return None
    mask = np.zeros((_CMP_H, _CMP_W), bool)
    for x0, y0, x1, y1 in regions:
        c0, c1 = int(math.floor(x0 * _CMP_W)), int(math.ceil(x1 * _CMP_W))
        r0, r1 = int(math.floor(y0 * _CMP_H)), int(math.ceil(y1 * _CMP_H))
        mask[max(0, r0):min(_CMP_H, r1), max(0, c0):min(_CMP_W, c1)] = True
    return mask


def _edges(frame, mask=None):
    """比較用：縮小してエッジ（文字や図の輪郭）を抽出する。除外領域のエッジは消す。
    返り値は (エッジ画像, 少し太らせたエッジ画像, エッジ画素数)"""
    g = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    g = cv2.resize(g, (_CMP_W, _CMP_H), interpolation=cv2.INTER_AREA)
    e = cv2.Canny(g, 60, 150)
    if mask is not None:
        e[mask] = 0
    return e > 0, cv2.dilate(e, _KERNEL) > 0, int(np.count_nonzero(e))


def _lost(a, b) -> float:
    """a にあった内容のうち、b で消えた割合（0〜1）。
    文字の追加（箇条書きの段階表示や書き込み）では 0 に近く、
    スライドが切り替わると大きくなる。"""
    ea, _, na = a
    _, db, nb = b
    if na < _MIN_EDGES:
        return 0.0 if nb < _MIN_EDGES else 1.0   # 無地 → 内容あり は「変化」
    return float(np.count_nonzero(ea & ~db)) / na


class _Run:
    """前後のフレームでほとんど変化がない、ひと続きの区間"""

    def __init__(self, t, frame, e, margin):
        self.start = t
        self.edges = e                      # 最新のエッジ
        self.margin = margin
        self.samples = deque([(t, frame)])  # 終わり側の余裕分だけ保持（メモリ節約）
        self.head = (t, frame)              # 開始から margin 秒後の時点で表示されている画面

    def add(self, t, frame, e):
        self.edges = e
        self.samples.append((t, frame))
        if t <= self.start + self.margin:
            self.head = (t, frame)
        while len(self.samples) >= 2 and self.samples[1][0] <= t - self.margin:
            self.samples.popleft()

    def _shown_at(self, x):
        """時刻 x に表示されている画面（x 以前で最も新しいフレーム）"""
        ok = [smp for smp in self.samples if smp[0] <= x + 1e-9]
        return max(ok, key=lambda smp: smp[0]) if ok else self.samples[0]

    def pick(self, end, mode):
        """候補にする (時刻, 画面)。時刻は切り替わりの瞬間から margin 秒離す。
        区間が短すぎて両側に margin 秒取れないときは、区間の真ん中にする。
        VFR動画では指定の時刻ちょうどのフレームがないことがあるので、
        「その時刻に表示されているフレーム」を返す。"""
        if end - self.start < 2 * self.margin:
            x = (self.start + end) / 2
            return x, self._shown_at(x)[1]
        if mode == "after":
            return self.start + self.margin, self.head[1]
        x = end - self.margin
        return x, self._shown_at(x)[1]


def detect_slides(path, params: DetectParams, should_stop=lambda: False,
                  on_progress=lambda pct: None):
    """スライド候補を (動画内の時刻[秒], 元解像度フレーム) で順に yield する。"""
    try:
        container = av.open(str(path))
        stream = container.streams.video[0]
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"動画を開けませんでした: {e}") from e
    stream.thread_type = "AUTO"
    start, duration, fps = _stream_info(container, stream)
    stable_thr = params.change_ratio / 3   # 「安定」の判定は切り替わり判定より厳しめ
    thr = params.change_ratio
    margin = max(0.0, params.margin_sec)
    mask = make_exclude_mask(params.exclude)
    if mask is not None and mask.mean() > 0.95:
        raise RuntimeError("除外領域が画面のほぼ全体を覆っています")

    emitted = []      # 候補にしたスライドのエッジ（重複防止用）
    seg = None        # 現在のスライド {"ref": エッジ, "pick": (時刻, 画面)}

    def flush(sg):
        if sg is None or sg["ref"][2] < _MIN_EDGES:  # 真っ黒・真っ白など無地の画面は除外
            return None
        e = sg["ref"]
        for x in emitted:                         # 前に出たスライドに戻っただけなら除外
            if _lost(x, e) < thr and _lost(e, x) < thr:
                return None
        emitted.append(e)
        return sg["pick"]

    def close_run(run, end):
        """区間が終わった。十分長く安定していたら、スライドとして扱う"""
        nonlocal seg
        if end - run.start < params.stable_sec:
            return None                           # フェードやアニメーションの途中
        if seg is not None and _lost(seg["ref"], run.edges) <= thr:
            seg["ref"] = run.edges                # 同じスライド（内容が増えただけ）
            if params.pick == "before":           # 「直前」はスライドの最後の状態を使う
                seg["pick"] = run.pick(end, "before")
            return None
        out = flush(seg)                          # 切り替わり → 前のスライドを確定
        seg = {"ref": run.edges, "pick": run.pick(end, params.pick)}
        return out

    run = None
    last_proc = None
    last_t = 0.0
    last_pct = -1
    interval = params.interval_sec - 1e-6

    def handle(t, f):
        """1枚のフレームを調べる。前のスライドが確定したら (時刻, 画面) を返す"""
        nonlocal run, last_proc
        frame = f.to_ndarray(format="bgr24")
        last_proc = t
        e = _edges(frame, mask)
        if run is not None and _lost(run.edges, e) < stable_thr \
                and _lost(e, run.edges) < stable_thr:
            run.add(t, frame, e)
            return None
        out = close_run(run, t) if run is not None else None
        run = _Run(t, frame, e, margin)
        return out

    pending = None    # 1つ前のフレーム。次のフレームの時刻を見てから、調べるか決める
    try:
        for f in container.decode(stream):
            if should_stop():
                break
            if f.time is None:
                continue
            t = max(f.time - start, last_t)       # 動画の先頭を 0 秒とした時刻
            last_t = t
            if pending is not None:
                pt, pf = pending
                # 一定間隔ごとに調べる。ただし、長く表示され続けるフレーム（VFR動画で
                # 画面が止まっている間のフレーム）は、間隔に関係なく必ず調べる
                if last_proc is None or pt - last_proc >= interval or t - pt >= interval:
                    out = handle(pt, pf)
                    if out:
                        yield out
            pending = (t, f)

            if duration > 0:
                pct = min(100, int(t * 100 / duration))
                if pct != last_pct:
                    last_pct = pct
                    on_progress(pct)
        if not should_stop() and pending is not None:
            out = handle(*pending)                # 最後のフレーム
            if out:
                yield out
        if not should_stop():
            if run is not None:                   # 最後の区間は動画の終わりまで続いたとみなす
                out = close_run(run, max(duration, last_t + 1.0 / fps))
                if out:
                    yield out
            out = flush(seg)                      # 最後のスライド
            if out:
                yield out
            on_progress(100)
    finally:
        container.close()


# ============================================================
# PDF書き出し（GUIに依存しない部分）
# ============================================================
@dataclass
class ExportSettings:
    page: str = "slide"      # "slide" / "a4" / "image"
    max_width: int = 1920    # 画像の最大幅（px）。0 は元のまま。拡大はしない
    fmt: str = "jpeg"        # "jpeg"（容量小） / "png"（劣化なし）
    quality: int = 85        # JPEG画質 (30〜100)


PAGE_CHOICES = [
    ("slide", "スライド（幅10インチ・PowerPointと同じ）"),
    ("a4", "A4 横（印刷向け）"),
    ("image", "画像の画素数に合わせる（以前の動作）"),
]
WIDTH_CHOICES = [
    (0, "元のまま"),
    (1920, "1920 px（フルHD）"),
    (1600, "1600 px"),
    (1280, "1280 px（HD）"),
    (960, "960 px（軽量）"),
]
FORMAT_CHOICES = [
    ("jpeg", "JPEG（容量が小さい・おすすめ）"),
    ("png", "PNG（劣化なし・容量が大きい）"),
]


def encode_for_pdf(png: bytes, st: ExportSettings):
    """保存用の画像（PNGバイト列）を、設定に合わせて縮小・再圧縮する。
    返り値は (画像バイト列, 幅, 高さ)"""
    img = cv2.imdecode(np.frombuffer(png, np.uint8), cv2.IMREAD_COLOR)
    h, w = img.shape[:2]
    if st.max_width and w > st.max_width:
        nh = max(1, round(h * st.max_width / w))
        img = cv2.resize(img, (st.max_width, nh), interpolation=cv2.INTER_AREA)
        h, w = img.shape[:2]
    if st.fmt == "jpeg":
        ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, int(st.quality),
                                             cv2.IMWRITE_JPEG_OPTIMIZE, 1])
    else:
        ok, buf = cv2.imencode(".png", img, [cv2.IMWRITE_PNG_COMPRESSION, 9])
    if not ok:
        raise RuntimeError("画像の変換に失敗しました")
    return buf.tobytes(), w, h


def page_size(st: ExportSettings, w: int, h: int):
    """ページの大きさ（ポイント, 1pt = 1/72 インチ）"""
    if st.page == "slide":
        return 720.0, 720.0 * h / w          # 幅10インチ。高さは画像の縦横比に合わせる
    if st.page == "a4":
        return 841.89, 595.28                # A4 横 (297 × 210 mm)
    return float(w), float(h)


def write_pdf(path, pngs, st: ExportSettings, on_progress=lambda k, n: None):
    doc = pymupdf.open()
    n = len(pngs)
    try:
        for k, png in enumerate(pngs):
            data, w, h = encode_for_pdf(png, st)
            pw, ph = page_size(st, w, h)
            page = doc.new_page(width=pw, height=ph)
            sc = min(pw / w, ph / h)               # 縦横比を保ってページ中央に配置
            iw, ih = w * sc, h * sc
            x, y = (pw - iw) / 2, (ph - ih) / 2
            page.insert_image(pymupdf.Rect(x, y, x + iw, y + ih), stream=data)
            on_progress(k + 1, n)
        doc.save(str(path), garbage=3, deflate=True)
    finally:
        doc.close()


class DetectThread(QThread):
    found = Signal(float, object)    # 動画内の時刻[秒], フレーム(BGR)
    progress = Signal(int)
    failed = Signal(str)

    def __init__(self, path, params: DetectParams, parent=None):
        super().__init__(parent)
        self.path = path
        self.params = params

    def run(self):
        try:
            for t, frame in detect_slides(
                    self.path, self.params,
                    should_stop=self.isInterruptionRequested,
                    on_progress=self.progress.emit):
                self.found.emit(t, frame)
        except Exception as e:  # noqa: BLE001
            self.failed.emit(str(e))


# ============================================================
# GUI
# ============================================================
class PreviewLabel(QLabel):
    """フレームを表示し、除外領域をドラッグで描けるラベル"""
    regions_changed = Signal()

    def __init__(self, text=""):
        super().__init__(text)
        self.regions = []          # [(x0, y0, x1, y1)] 画面に対する割合
        self.draw_mode = False
        self._start = None         # ドラッグ開始点（割合）
        self._now = None
        self.setMouseTracking(False)

    def set_draw_mode(self, on: bool):
        self.draw_mode = on
        self.setCursor(Qt.CrossCursor if on else Qt.ArrowCursor)
        self._start = self._now = None
        self.update()

    def clear_regions(self):
        self.regions.clear()
        self.update()
        self.regions_changed.emit()

    # 表示中の画像が、ラベル内のどこに描かれているか
    def _image_rect(self):
        pm = self.pixmap()
        if pm is None or pm.isNull():
            return None
        w, h = pm.width(), pm.height()
        return QRectF((self.width() - w) / 2, (self.height() - h) / 2, w, h)

    def _to_norm(self, pos, clamp=True):
        r = self._image_rect()
        if r is None:
            return None
        x = (pos.x() - r.x()) / r.width()
        y = (pos.y() - r.y()) / r.height()
        if not clamp and not (0 <= x <= 1 and 0 <= y <= 1):
            return None
        return QPointF(min(max(x, 0.0), 1.0), min(max(y, 0.0), 1.0))

    def _to_widget(self, x0, y0, x1, y1):
        r = self._image_rect()
        return QRectF(r.x() + x0 * r.width(), r.y() + y0 * r.height(),
                      (x1 - x0) * r.width(), (y1 - y0) * r.height())

    def mousePressEvent(self, ev):
        p = self._to_norm(ev.position(), clamp=False)
        if p is None:
            return super().mousePressEvent(ev)
        if ev.button() == Qt.RightButton:
            # 右クリック：その位置の除外領域を削除（後から描いたものを優先）
            for k in range(len(self.regions) - 1, -1, -1):
                x0, y0, x1, y1 = self.regions[k]
                if x0 <= p.x() <= x1 and y0 <= p.y() <= y1:
                    del self.regions[k]
                    self.update()
                    self.regions_changed.emit()
                    break
        elif ev.button() == Qt.LeftButton and self.draw_mode:
            self._start = self._now = p
            self.update()

    def mouseMoveEvent(self, ev):
        if self._start is not None:
            self._now = self._to_norm(ev.position())
            self.update()

    def mouseReleaseEvent(self, ev):
        if self._start is not None and ev.button() == Qt.LeftButton:
            end = self._to_norm(ev.position())
            x0, x1 = sorted((self._start.x(), end.x()))
            y0, y1 = sorted((self._start.y(), end.y()))
            self._start = self._now = None
            if (x1 - x0) > 0.01 and (y1 - y0) > 0.01:     # 小さすぎるドラッグは無視
                self.regions.append((x0, y0, x1, y1))
                self.regions_changed.emit()
            self.update()

    def paintEvent(self, ev):
        super().paintEvent(ev)
        if self._image_rect() is None:
            return
        qp = QPainter(self)
        qp.setRenderHint(QPainter.Antialiasing)
        pen = QPen(QColor(255, 70, 70), 2, Qt.DashLine)
        for reg in self.regions:
            rect = self._to_widget(*reg)
            qp.fillRect(rect, QColor(255, 70, 70, 70))
            qp.setPen(pen)
            qp.drawRect(rect)
            qp.drawText(rect.adjusted(4, 2, 0, 0), Qt.AlignLeft | Qt.AlignTop, "除外")
        if self._start is not None and self._now is not None:
            x0, x1 = sorted((self._start.x(), self._now.x()))
            y0, y1 = sorted((self._start.y(), self._now.y()))
            rect = self._to_widget(x0, y0, x1, y1)
            qp.fillRect(rect, QColor(255, 210, 0, 60))
            qp.setPen(QPen(QColor(255, 210, 0), 2))
            qp.drawRect(rect)
        qp.end()


class ExportDialog(QDialog):
    """PDF保存の設定。予想ファイルサイズをその場で表示する。"""

    def __init__(self, pngs, st: ExportSettings, parent=None):
        super().__init__(parent)
        self.setWindowTitle("PDF保存の設定")
        self.pngs = pngs

        def combo(choices, value):
            cb = QComboBox()
            for key, text in choices:
                cb.addItem(text, key)
            idx = cb.findData(value)
            cb.setCurrentIndex(idx if idx >= 0 else 0)
            return cb

        self.page_cb = combo(PAGE_CHOICES, st.page)
        self.width_cb = combo(WIDTH_CHOICES, st.max_width)
        self.fmt_cb = combo(FORMAT_CHOICES, st.fmt)
        self.quality_sp = QSpinBox()
        self.quality_sp.setRange(30, 100)
        self.quality_sp.setValue(st.quality)
        self.quality_sp.setToolTip("大きいほど高画質・大容量。文字のスライドなら 80〜90 が目安です")
        self.info = QLabel()
        self.info.setWordWrap(True)

        form = QFormLayout()
        form.addRow("ページサイズ", self.page_cb)
        form.addRow("画像の解像度（幅）", self.width_cb)
        form.addRow("画像形式", self.fmt_cb)
        form.addRow("JPEG画質", self.quality_sp)
        form.addRow(self.info)

        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Ok).setText("保存先を選ぶ…")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)

        lay = QVBoxLayout(self)
        lay.addLayout(form)
        lay.addWidget(bb)

        # 設定を変えたら少し待ってから予想サイズを計算し直す
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(150)
        self.timer.timeout.connect(self.update_info)
        for cb in (self.page_cb, self.width_cb, self.fmt_cb):
            cb.currentIndexChanged.connect(self._changed)
        self.quality_sp.valueChanged.connect(self._changed)
        self._changed()
        self.update_info()

    def settings(self) -> ExportSettings:
        return ExportSettings(
            page=self.page_cb.currentData(),
            max_width=int(self.width_cb.currentData()),
            fmt=self.fmt_cb.currentData(),
            quality=self.quality_sp.value(),
        )

    def _changed(self):
        self.quality_sp.setEnabled(self.fmt_cb.currentData() == "jpeg")
        self.timer.start()

    def update_info(self):
        st = self.settings()
        n = len(self.pngs)
        # 最大3枚を実際に変換して、1枚あたりの平均から全体のサイズを見積もる
        picks = sorted({0, n // 2, n - 1}) if n else []
        sizes, w, h = [], 0, 0
        for k in picks:
            data, w, h = encode_for_pdf(self.pngs[k], st)
            sizes.append(len(data))
        est = (sum(sizes) / len(sizes)) * n if sizes else 0
        pw, ph = page_size(st, w, h) if w else (0, 0)
        self.info.setText(
            f"出力画像：{w} × {h} px　／　ページ：{pw / 72 * 25.4:.0f} × {ph / 72 * 25.4:.0f} mm\n"
            f"予想サイズ：約 {est / 1024 / 1024:.1f} MB（{n} ページ）")


ROLE_ID = Qt.UserRole          # captures 辞書のキー
ROLE_TIME = Qt.UserRole + 1    # 動画内の時刻（ミリ秒）
ROLE_AUTO = Qt.UserRole + 2    # 自動検出による候補か


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("講義動画 → スライドPDF")
        self.resize(1100, 700)          # 実際の大きさは show_initial() で画面に合わせる

        self.reader = None             # VideoReader
        self.video_path = None
        self.fps = 30.0
        self.duration_ms = 0
        self.current_frame = None      # 現在表示中のフレーム（BGR, 元解像度）
        self.current_ms = 0            # 現在表示中のフレームの時刻（ミリ秒）
        self.captures = {}             # id -> PNGバイト列（元解像度）
        self.next_id = 0
        self.detect_thread = None

        self._build_ui()
        self._build_shortcuts()

        # シークバーをドラッグ中に毎回読み込むと重いので、少し間引く
        self.seek_timer = QTimer(self)
        self.seek_timer.setSingleShot(True)
        self.seek_timer.setInterval(30)
        self.seek_timer.timeout.connect(self._load_slider_frame)

    # ---------------- UI ----------------
    def _build_ui(self):
        # 左側：プレビュー＋操作
        self.preview = PreviewLabel("「動画を開く」から動画を選択してください")
        self.preview.regions_changed.connect(self._update_region_label)
        self.preview.setAlignment(Qt.AlignCenter)
        self.preview.setMinimumSize(320, 180)
        self.preview.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Ignored)
        self.preview.setStyleSheet("background:#202020; color:#bbbbbb;")

        self.slider = QSlider(Qt.Horizontal)
        self.slider.setEnabled(False)
        self.slider.setFocusPolicy(Qt.NoFocus)   # 矢印キーはショートカットで処理する
        self.slider.valueChanged.connect(lambda _: self.seek_timer.start())

        self.time_label = QLabel("00:00 / 00:00")

        self.open_btn = QPushButton("動画を開く")
        self.open_btn.clicked.connect(self.open_video)
        self.add_btn = QPushButton("このフレームを追加 (Enter)")
        self.add_btn.setEnabled(False)
        self.add_btn.clicked.connect(self.add_current_frame)

        ctrl = QHBoxLayout()
        ctrl.addWidget(self.open_btn)
        ctrl.addStretch()
        ctrl.addWidget(self.time_label)
        ctrl.addStretch()
        ctrl.addWidget(self.add_btn)

        # 自動検出の操作行
        self.detect_btn = QPushButton("自動検出")
        self.detect_btn.setEnabled(False)
        self.detect_btn.clicked.connect(self.start_detection)
        self.stop_btn = QPushButton("中止")
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self.stop_detection)

        def spin(val, lo, hi, step, suffix, tip):
            sp = QDoubleSpinBox()
            sp.setRange(lo, hi)
            sp.setSingleStep(step)
            sp.setDecimals(1)
            sp.setValue(val)
            sp.setSuffix(suffix)
            sp.setToolTip(tip)
            sp.setFocusPolicy(Qt.ClickFocus)
            return sp

        self.ratio_spin = spin(20.0, 3.0, 80.0, 1.0, " %",
                               "前の画面の文字や図がこの割合以上消えたら、別スライドとみなします。\n"
                               "小さくすると細かい切り替わりも拾い、大きくすると大きな切り替わりだけを拾います。\n"
                               "（箇条書きの追加や書き込みは「消えない」ので、同じスライドの続きとして扱われます）")
        self.interval_spin = spin(0.5, 0.1, 5.0, 0.1, " 秒",
                                  "何秒ごとにフレームを調べるか")
        self.stable_spin = spin(1.0, 0.2, 10.0, 0.5, " 秒",
                                "この時間、画面が止まっていたら安定とみなします")
        self.pick_cb = QComboBox()
        self.pick_cb.addItem("次の切り替わりの直前", "before")
        self.pick_cb.addItem("切り替わりの直後", "after")
        self.pick_cb.setToolTip(
            "候補にする場面を選びます。\n"
            "・次の切り替わりの直前：スライドの最後の状態（箇条書きが全部出そろった状態）\n"
            "・切り替わりの直後：スライドが表示された最初の状態")
        self.pick_cb.setFocusPolicy(Qt.ClickFocus)
        self.margin_spin = spin(1.0, 0.0, 10.0, 0.5, " 秒",
                                "切り替わりの瞬間から、この秒数だけ離れた場面を候補にします。\n"
                                "候補の画像と、ダブルクリックで移動した先の画面が違う場合は大きくしてください。")
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self._load_detect_settings()

        det = QHBoxLayout()
        det.addWidget(self.detect_btn)
        det.addWidget(self.stop_btn)
        det.addWidget(self.progress, 1)

        det2 = QHBoxLayout()
        det2.addWidget(QLabel("感度"))
        det2.addWidget(self.ratio_spin)
        det2.addWidget(QLabel("間隔"))
        det2.addWidget(self.interval_spin)
        det2.addWidget(QLabel("安定時間"))
        det2.addWidget(self.stable_spin)
        det2.addStretch()

        det3 = QHBoxLayout()
        det3.addWidget(QLabel("採用する場面"))
        det3.addWidget(self.pick_cb)
        det3.addWidget(QLabel("余裕"))
        det3.addWidget(self.margin_spin)
        det3.addStretch()

        # 除外領域の操作行
        self.region_btn = QPushButton("除外領域を描く")
        self.region_btn.setCheckable(True)
        self.region_btn.setEnabled(False)
        self.region_btn.setToolTip("オンにしてプレビュー上をドラッグすると、その範囲を検出から外します。\n"
                                   "右クリックで個別に削除できます。")
        self.region_btn.toggled.connect(self.preview.set_draw_mode)
        self.clear_region_btn = QPushButton("除外領域をクリア")
        self.clear_region_btn.clicked.connect(self.preview.clear_regions)
        self.region_label = QLabel()
        reg = QHBoxLayout()
        reg.addWidget(self.region_btn)
        reg.addWidget(self.clear_region_btn)
        reg.addWidget(self.region_label)
        reg.addStretch()
        self._update_region_label()

        left = QVBoxLayout()
        left.addWidget(self.preview, 1)
        left.addWidget(self.slider)
        left.addLayout(ctrl)
        left.addLayout(det)
        left.addLayout(det2)
        left.addLayout(det3)
        left.addLayout(reg)

        # 右側：選択済みリスト
        self.list = QListWidget()
        self.list.setIconSize(QSize(192, 108))
        self.list.setDragDropMode(QAbstractItemView.InternalMove)
        self.list.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.list.itemDoubleClicked.connect(self._jump_to_item)
        self.list.setMinimumWidth(220)

        self.count_label = QLabel("選択済み: 0 枚")
        self.del_btn = QPushButton("選択項目を削除 (Delete)")
        self.del_btn.clicked.connect(self.delete_selected)
        self.replace_btn = QPushButton("表示中のフレームで置き換え (R)")
        self.replace_btn.setToolTip("一覧で選択中の項目の画像を、左に表示しているフレームに置き換えます。\n"
                                    "ダブルクリックで移動 → 矢印キーで調整 → R、の順に使います。")
        self.replace_btn.clicked.connect(self.replace_selected)
        self.save_btn = QPushButton("PDF保存")
        self.save_btn.clicked.connect(self.save_pdf)

        right = QVBoxLayout()
        right.addWidget(self.count_label)
        right.addWidget(self.list, 1)
        right.addWidget(self.replace_btn)
        right.addWidget(self.del_btn)
        right.addWidget(self.save_btn)

        root = QHBoxLayout()
        root.addLayout(left, 3)
        root.addLayout(right, 1)
        w = QWidget()
        w.setLayout(root)
        self.setCentralWidget(w)

        # 動画ファイルのドラッグ＆ドロップ（一覧の上に落としても受け付ける）
        self.setAcceptDrops(True)
        self.list.viewport().installEventFilter(self)

    def _build_shortcuts(self):
        def sc(key, func):
            s = QShortcut(QKeySequence(key), self)
            s.setContext(Qt.ApplicationShortcut)
            s.activated.connect(func)

        sc("Left", lambda: self.step_seconds(-1))
        sc("Right", lambda: self.step_seconds(1))
        sc("Shift+Left", lambda: self.step_seconds(-10))
        sc("Shift+Right", lambda: self.step_seconds(10))
        sc(",", lambda: self.step_frames(-1))
        sc(".", lambda: self.step_frames(1))
        sc("Return", self.add_current_frame)
        sc("Enter", self.add_current_frame)
        sc("Space", self.add_current_frame)
        sc("Delete", self.delete_selected)
        sc("Escape", lambda: self.region_btn.setChecked(False))
        sc("R", self.replace_selected)

    # ---------------- 動画 ----------------
    def open_video(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "動画を開く", "",
            "動画ファイル (*.mp4 *.mkv *.avi *.mov *.webm *.m4v);;すべてのファイル (*)")
        if path:
            self.load_video(path)

    def load_video(self, path: str) -> bool:
        if self.detect_thread is not None:
            QMessageBox.information(self, "動画を開く", "自動検出の実行中は、別の動画を開けません。")
            return False
        if self.list.count() > 0:
            ans = QMessageBox.question(
                self, "動画を開く",
                f"一覧に {self.list.count()} 件の項目があります。\n"
                "クリアしてから新しい動画を開きますか？\n"
                "（「いいえ」を選ぶと一覧を残したまま開きます）",
                QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel)
            if ans == QMessageBox.Cancel:
                return False
            clear_list = ans == QMessageBox.Yes
        else:
            clear_list = False
        try:
            reader = VideoReader(path)
            if reader.frame_at(0) is None:
                raise RuntimeError("フレームを読み込めません")
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(
                self, "エラー",
                f"動画を開けませんでした。\n対応していない形式か、ファイルが壊れている可能性があります。\n\n{e}")
            return False

        if self.reader is not None:
            self.reader.close()
        if clear_list:
            self.list.clear()
            self.captures.clear()
            self._update_count()
        self.reader = reader
        self.video_path = Path(path)
        self.fps = reader.fps
        self.duration_ms = int(reader.duration * 1000)

        self.slider.blockSignals(True)
        self.slider.setRange(0, max(0, self.duration_ms))   # シークバーの単位はミリ秒
        self.slider.setSingleStep(max(1, round(1000 / self.fps)))
        self.slider.setPageStep(10000)
        self.slider.setValue(0)
        self.slider.blockSignals(False)
        self.slider.setEnabled(True)
        self.add_btn.setEnabled(True)
        self.detect_btn.setEnabled(True)
        self.region_btn.setEnabled(True)
        self.progress.setValue(0)
        self.setWindowTitle(f"講義動画 → スライドPDF - {self.video_path.name}")
        self.show_frame(0)
        return True

    def show_frame(self, ms: int):
        """時刻 ms（ミリ秒）に表示されているフレームを表示する"""
        if self.reader is None:
            return
        res = self.reader.frame_at(ms / 1000)
        if res is None:
            return
        t, frame = res
        self.current_frame = frame
        self.current_ms = int(round(t * 1000))   # 実際に表示しているフレームの時刻
        self._update_preview()
        self.time_label.setText(
            f"{format_time(self.current_ms / 1000)} / {format_time(self.duration_ms / 1000)}")

    def _load_slider_frame(self):
        self.show_frame(self.slider.value())

    def _update_preview(self):
        if self.current_frame is None:
            return
        pix = QPixmap.fromImage(bgr_to_qimage(self.current_frame))
        self.preview.setPixmap(
            pix.scaled(self.preview.size(), Qt.KeepAspectRatio,
                       Qt.SmoothTransformation))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._update_preview()

    def _go(self, ms: int):
        """時刻 ms へ移動してすぐ表示する（キー操作・ダブルクリック用）"""
        ms = max(0, min(self.duration_ms, int(ms)))
        self.seek_timer.stop()
        self.slider.blockSignals(True)
        self.slider.setValue(ms)
        self.slider.blockSignals(False)
        self.show_frame(ms)

    def step_frames(self, n: int):
        """前後のフレームへ移動（VFR動画でも、実際に記録されている次／前のフレームへ）"""
        if self.reader is None or self.current_frame is None:
            return
        self.seek_timer.stop()
        self.show_frame(self.slider.value())      # 表示と内部状態をそろえる
        if n > 0:
            t = self.reader.next_time()
            if t is not None:
                self._go(round(t * 1000))
        else:
            res = self.reader.frame_at(max(0.0, self.current_ms / 1000 - 0.004))
            if res is not None:
                self._go(round(res[0] * 1000))

    def step_seconds(self, sec: float):
        if self.reader is not None:
            self._go(self.slider.value() + round(sec * 1000))

    # ---------------- 自動検出 ----------------
    def start_detection(self):
        if self.video_path is None or self.detect_thread is not None:
            return
        auto_rows = [r for r in range(self.list.count())
                     if self.list.item(r).data(ROLE_AUTO)]
        if auto_rows:
            ans = QMessageBox.question(
                self, "自動検出",
                f"前回の自動検出の候補が {len(auto_rows)} 件あります。\n"
                "削除してから検出しますか？（手動で追加した項目は残ります）",
                QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel)
            if ans == QMessageBox.Cancel:
                return
            if ans == QMessageBox.Yes:
                for r in reversed(auto_rows):
                    item = self.list.takeItem(r)
                    self.captures.pop(item.data(ROLE_ID), None)
                self._update_count()

        params = DetectParams(
            change_ratio=self.ratio_spin.value() / 100,
            interval_sec=self.interval_spin.value(),
            stable_sec=self.stable_spin.value(),
            exclude=tuple(self.preview.regions),
            pick=self.pick_cb.currentData(),
            margin_sec=self.margin_spin.value(),
        )
        self._save_detect_settings()
        self.detect_thread = DetectThread(str(self.video_path), params, self)
        self.detect_thread.found.connect(self._on_detected)
        self.detect_thread.progress.connect(self.progress.setValue)
        self.detect_thread.failed.connect(
            lambda msg: QMessageBox.critical(self, "エラー", f"自動検出に失敗しました:\n{msg}"))
        self.detect_thread.finished.connect(self._on_detection_finished)
        self._set_detecting(True)
        self.detect_found = 0
        self.progress.setValue(0)
        self.detect_thread.start()

    def stop_detection(self):
        if self.detect_thread is not None:
            self.detect_thread.requestInterruption()

    def _on_detected(self, t: float, frame):
        self.add_frame(frame, int(round(t * 1000)), auto=True)
        self.detect_found += 1

    def _load_detect_settings(self):
        q = QSettings("slide_capture", "slide_capture")
        try:
            self.ratio_spin.setValue(float(q.value("detect/ratio", self.ratio_spin.value())))
            self.interval_spin.setValue(float(q.value("detect/interval", self.interval_spin.value())))
            self.stable_spin.setValue(float(q.value("detect/stable", self.stable_spin.value())))
            self.margin_spin.setValue(float(q.value("detect/margin", self.margin_spin.value())))
            idx = self.pick_cb.findData(str(q.value("detect/pick", "before")))
            self.pick_cb.setCurrentIndex(max(0, idx))
        except (TypeError, ValueError):
            pass

    def _save_detect_settings(self):
        q = QSettings("slide_capture", "slide_capture")
        q.setValue("detect/ratio", self.ratio_spin.value())
        q.setValue("detect/interval", self.interval_spin.value())
        q.setValue("detect/stable", self.stable_spin.value())
        q.setValue("detect/margin", self.margin_spin.value())
        q.setValue("detect/pick", self.pick_cb.currentData())

    def _on_detection_finished(self):
        stopped = self.detect_thread.isInterruptionRequested()
        self.detect_thread.deleteLater()
        self.detect_thread = None
        self._set_detecting(False)
        msg = "中止しました" if stopped else "完了しました"
        self.statusBar().showMessage(
            f"自動検出を{msg}：候補 {self.detect_found} 件", 8000)

    def _set_detecting(self, on: bool):
        self.detect_btn.setEnabled(not on)
        self.stop_btn.setEnabled(on)
        self.open_btn.setEnabled(not on)
        for wdg in (self.ratio_spin, self.interval_spin, self.stable_spin,
                    self.pick_cb, self.margin_spin, self.clear_region_btn):
            wdg.setEnabled(not on)
        if on:
            self.region_btn.setChecked(False)
        self.region_btn.setEnabled(not on)

    def _update_region_label(self):
        n = len(self.preview.regions)
        if n == 0:
            self.region_label.setText("除外領域なし")
        else:
            area = make_exclude_mask(self.preview.regions).mean() * 100
            self.region_label.setText(f"除外領域 {n} 個（画面の {area:.0f}%）")

    # ---------------- 選択リスト ----------------
    def add_current_frame(self):
        if self.current_frame is not None:
            self.add_frame(self.current_frame, self.current_ms, auto=False)

    def add_frame(self, frame, ms: int, auto: bool):
        ok, buf = cv2.imencode(".png", frame)
        if not ok:
            return
        cid = self.next_id
        self.next_id += 1
        self.captures[cid] = buf.tobytes()

        thumb = QPixmap.fromImage(bgr_to_qimage(frame)).scaled(
            192, 108, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        label = format_time(ms / 1000) + ("  （自動）" if auto else "")
        item = QListWidgetItem(QIcon(thumb), label)
        item.setData(ROLE_ID, cid)
        item.setData(ROLE_TIME, ms)
        item.setData(ROLE_AUTO, auto)

        if auto:
            # 自動候補は時刻順の位置に差し込む
            row = self.list.count()
            for r in range(self.list.count()):
                if (self.list.item(r).data(ROLE_TIME) or 0) > ms:
                    row = r
                    break
            self.list.insertItem(row, item)
            self.list.scrollToItem(item)
        else:
            self.list.addItem(item)
            self.list.scrollToBottom()
        self._update_count()

    def _set_item_frame(self, item: QListWidgetItem, frame, ms: int, label_suffix: str):
        ok, buf = cv2.imencode(".png", frame)
        if not ok:
            return False
        self.captures[item.data(ROLE_ID)] = buf.tobytes()
        thumb = QPixmap.fromImage(bgr_to_qimage(frame)).scaled(
            192, 108, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        item.setIcon(QIcon(thumb))
        item.setText(format_time(ms / 1000) + label_suffix)
        item.setData(ROLE_TIME, ms)
        return True

    def replace_selected(self):
        """一覧で選択中の項目（1件）を、表示中のフレームで置き換える"""
        items = self.list.selectedItems()
        if self.current_frame is None or not items:
            return
        if len(items) > 1:
            QMessageBox.information(self, "置き換え", "置き換える項目を1つだけ選択してください。")
            return
        item = items[0]
        suffix = "  （修正）"
        if self._set_item_frame(item, self.current_frame, self.current_ms, suffix):
            self.statusBar().showMessage(
                f"{format_time(self.current_ms / 1000)} のフレームで置き換えました", 5000)

    def delete_selected(self):
        for item in self.list.selectedItems():
            self.captures.pop(item.data(ROLE_ID), None)
            self.list.takeItem(self.list.row(item))
        self._update_count()

    def _jump_to_item(self, item: QListWidgetItem):
        """一覧の項目をダブルクリックすると、その時刻へ移動する"""
        ms = item.data(ROLE_TIME)
        if ms is not None and self.reader is not None:
            self.seek_timer.stop()
            self._go(int(ms))

    def _update_count(self):
        self.count_label.setText(f"選択済み: {self.list.count()} 枚")

    # ---------------- PDF ----------------
    def _ordered_pngs(self):
        return [self.captures[self.list.item(r).data(ROLE_ID)]
                for r in range(self.list.count())]

    def _load_export_settings(self) -> ExportSettings:
        q = QSettings("slide_capture", "slide_capture")
        d = ExportSettings()
        try:
            return ExportSettings(
                page=str(q.value("export/page", d.page)),
                max_width=int(q.value("export/max_width", d.max_width)),
                fmt=str(q.value("export/fmt", d.fmt)),
                quality=int(q.value("export/quality", d.quality)),
            )
        except (TypeError, ValueError):
            return d

    def _save_export_settings(self, st: ExportSettings):
        q = QSettings("slide_capture", "slide_capture")
        q.setValue("export/page", st.page)
        q.setValue("export/max_width", st.max_width)
        q.setValue("export/fmt", st.fmt)
        q.setValue("export/quality", st.quality)

    def save_pdf(self):
        if self.list.count() == 0:
            QMessageBox.information(self, "PDF保存", "フレームが選択されていません。")
            return
        pngs = self._ordered_pngs()
        dlg = ExportDialog(pngs, self._load_export_settings(), self)
        if dlg.exec() != QDialog.Accepted:
            return
        st = dlg.settings()
        self._save_export_settings(st)       # 次回も同じ設定で開く

        default = (self.video_path.with_suffix(".pdf").name
                   if self.video_path else "slides.pdf")
        path, _ = QFileDialog.getSaveFileName(
            self, "PDF保存", default, "PDF (*.pdf)")
        if not path:
            return

        prog = QProgressDialog("PDFを保存しています…", None, 0, len(pngs), self)
        prog.setWindowModality(Qt.WindowModal)
        prog.setMinimumDuration(300)

        def on_progress(k, n):
            prog.setValue(k)
            QApplication.processEvents()

        try:
            write_pdf(path, pngs, st, on_progress)
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, "エラー", f"PDFの保存に失敗しました:\n{e}")
            return
        finally:
            prog.close()
        size_mb = Path(path).stat().st_size / 1024 / 1024
        QMessageBox.information(
            self, "PDF保存",
            f"{len(pngs)} ページのPDFを保存しました（{size_mb:.1f} MB）。\n{path}")

    def write_pdf(self, path: str, st: ExportSettings = None):
        """一覧の順に PDF を書き出す（設定を省略すると既定値）"""
        write_pdf(path, self._ordered_pngs(), st or ExportSettings())

    # ---------------- ドラッグ＆ドロップ ----------------
    VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".mov", ".webm", ".m4v", ".wmv", ".flv", ".mpg", ".mpeg", ".ts"}

    def _dropped_path(self, mime):
        """ドロップされたものが動画ファイル1つならそのパス、そうでなければ None"""
        if not mime.hasUrls():
            return None
        files = [u.toLocalFile() for u in mime.urls() if u.isLocalFile()]
        if len(files) != 1:
            return None
        return files[0] if Path(files[0]).suffix.lower() in self.VIDEO_EXTS else None

    def dragEnterEvent(self, event):
        if self._dropped_path(event.mimeData()):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        self.dragEnterEvent(event)

    def dropEvent(self, event):
        path = self._dropped_path(event.mimeData())
        if path:
            event.acceptProposedAction()
            # ドロップ処理の中でダイアログを出すとドラッグ元のアプリが固まることがあるので、少し後で開く
            QTimer.singleShot(0, lambda: self.load_video(path))
        else:
            event.ignore()

    def eventFilter(self, obj, event):
        # 一覧の上に動画ファイルを落とした場合も開く（一覧の並び替えのドラッグはそのまま）
        if obj is self.list.viewport() and event.type() in (
                QEvent.DragEnter, QEvent.DragMove, QEvent.Drop):
            if self._dropped_path(event.mimeData()):
                {QEvent.DragEnter: self.dragEnterEvent, QEvent.DragMove: self.dragMoveEvent,
                 QEvent.Drop: self.dropEvent}[event.type()](event)
                return True
        return super().eventFilter(obj, event)

    # ---------------- ウィンドウの大きさ ----------------
    def show_initial(self):
        """前回終了時の位置・大きさ（最大化の状態も含む）で開く。
        初回や、前回の位置が今の画面に収まらない場合（モニターを外したときなど）は最大化して開く。"""
        q = QSettings("slide_capture", "slide_capture")
        geo = q.value("window/geometry")
        restored = False
        if geo is not None and self.restoreGeometry(geo):
            screen = QApplication.screenAt(self.geometry().center())
            restored = screen is not None and screen.availableGeometry().intersects(self.frameGeometry())
        if restored:
            self.show()
        else:
            screen = QApplication.primaryScreen()
            if screen is not None:     # 最大化を解除したときの大きさ（画面の 85%・中央）
                area = screen.availableGeometry()
                self.resize(int(area.width() * 0.85), int(area.height() * 0.85))
                self.move(area.center() - self.rect().center())
            self.showMaximized()

    def closeEvent(self, event):
        QSettings("slide_capture", "slide_capture").setValue("window/geometry", self.saveGeometry())
        if self.detect_thread is not None:
            self.detect_thread.requestInterruption()
            self.detect_thread.wait()
        if self.reader is not None:
            self.reader.close()
        super().closeEvent(event)


def _install_excepthook():
    """予期しないエラーをダイアログとログファイルに出す。
    exe（ウィンドウ版）ではコンソールがないため、これがないと無言で落ちてしまう。"""
    import traceback
    log = Path.home() / "slide_capture_error.log"

    def hook(etype, value, tb):
        text = "".join(traceback.format_exception(etype, value, tb))
        try:
            with open(log, "a", encoding="utf-8") as f:
                f.write(text + "\n")
        except OSError:
            pass
        if sys.__stderr__:
            sys.__stderr__.write(text)
        if QApplication.instance():
            QMessageBox.critical(None, "予期しないエラー",
                                 f"{value}\n\n詳細はログに保存しました:\n{log}")

    sys.excepthook = hook


def main():
    _install_excepthook()
    app = QApplication(sys.argv)
    app.setApplicationName("SlideCapture")
    win = MainWindow()
    win.show_initial()
    if len(sys.argv) > 1:          # python slide_capture.py lecture.mp4 でも開ける
        win.load_video(sys.argv[1])
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
