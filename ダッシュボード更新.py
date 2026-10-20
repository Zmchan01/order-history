# ダッシュボード更新.py
# ============================================
# 注文データはログイン後にブラウザが直接Supabaseの「注文履歴_表示用」
# ビューから取得する方式になったため、ここではHTMLへのデータ埋め込みは
# 行わない。このスクリプトの役割は「新しい注文に対応する商品画像を
# ローカルフォルダから探してimages/にコピーし、GitHubへpushする」ことだけ。
# ============================================

from dotenv import load_dotenv
import os
import re
import shutil
import subprocess
import psycopg2
from datetime import date
from PIL import Image

SITE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(SITE_DIR, ".env"))
# DB接続情報はPythonスクレイピングフォルダの.envと共通
load_dotenv(r"C:\Users\hzmzh\Desktop\Pythonスクレイピング\.env")

DB_CONFIG = {
    "host": os.getenv("SUPABASE_DB_HOST"),
    "dbname": os.getenv("SUPABASE_DB_NAME"),
    "user": os.getenv("SUPABASE_DB_USER"),
    "password": os.getenv("SUPABASE_DB_PASSWORD"),
    "port": int(os.getenv("SUPABASE_DB_PORT", "6543")),
}

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
GITHUB_REPO_URL = os.getenv("GITHUB_REPO_URL")
SITE_DIR_ABS = SITE_DIR
IMAGES_DIR = os.path.join(SITE_DIR, "images")

# 商品画像の検索先（優先順）。見つかった最初の1件を使う
IMAGE_SEARCH_PATHS = [
    lambda jan: os.path.join(r"D:\AmazonImg", f"{jan}a.jpg"),
    lambda jan: os.path.join(r"D:\他サイト商品画像\あみあみ", f"{jan}.jpg"),
    lambda jan: os.path.join(r"D:\他サイト商品画像\ハピネットオンライン", f"{jan}.jpg"),
]

# Amazonランキング情報.pyがASIN名で保存している画像フォルダ
# （リサーチタブの商品はJAN名の画像が無いことが多いため、ASIN経由でも探す）
AMAZON_RANKING_IMAGE_DIR = r"D:\Amazonランキング画像"


# 画像コピー対象を探すため、注文履歴にある全SKUだけを取得する
def fetch_all_skus():
    conn = psycopg2.connect(**DB_CONFIG)
    cur = conn.cursor()
    cur.execute('SELECT DISTINCT "SKU" FROM "注文履歴" WHERE "SKU" IS NOT NULL')
    rows = [r[0] for r in cur.fetchall()]
    cur.close()
    conn.close()
    return rows


# リサーチタブに表示される商品（売れ筋アリ=トップカート価格が定価より1000円以上高い）の
# JANを取得する。注文とは無関係のため、こちらは別途取得・同期する
RESEARCH_SALES_FLAG_THRESHOLD = 1000


def fetch_research_jans():
    conn = psycopg2.connect(**DB_CONFIG)
    cur = conn.cursor()
    cur.execute(
        'SELECT "JAN" FROM "stockdb_リサーチ_表示用" '
        'WHERE ("トップカート価格" - "定価") >= %s',
        (RESEARCH_SALES_FLAG_THRESHOLD,)
    )
    rows = [r[0] for r in cur.fetchall()]
    cur.close()
    conn.close()
    return rows


# 指定したJAN群に対応するASIN（Amazon情報テーブル）を取得する。
# 1つのJANに複数ASINがある場合は先に見つかった1件を使う
def fetch_jan_to_asin(jans):
    if not jans:
        return {}
    conn = psycopg2.connect(**DB_CONFIG)
    cur = conn.cursor()
    cur.execute(
        'SELECT "JAN", "ASIN" FROM "Amazon情報" WHERE "JAN" = ANY(%s) AND "ASIN" IS NOT NULL',
        (jans,)
    )
    jan_to_asin = {}
    for jan, asin in cur.fetchall():
        jan_to_asin.setdefault(jan, asin)
    cur.close()
    conn.close()
    return jan_to_asin


# この更新スクリプトはタスクスケジューラから30分おきに実行されるが、
# 売れ筋判定は1日1回（日付が変わった後の最初の1回）だけ実行したいため、
# 最後に実行した日付をローカルファイルに記録して判定する
SALES_JUDGMENT_MARKER_PATH = os.path.join(SITE_DIR, "売れ筋判定_最終実行日.txt")


def should_run_sales_judgment():
    today = date.today().isoformat()
    if os.path.exists(SALES_JUDGMENT_MARKER_PATH):
        with open(SALES_JUDGMENT_MARKER_PATH, encoding="utf-8") as f:
            if f.read().strip() == today:
                return False
    return True


def mark_sales_judgment_done():
    with open(SALES_JUDGMENT_MARKER_PATH, "w", encoding="utf-8") as f:
        f.write(date.today().isoformat())


# リサーチタブと同じ基準（トップカート価格-定価が閾値以上）でstockdb.売れ筋判定を更新する。
# 手動で「売れ筋確定」にされたJANは自動判定で上書きしない。
# 実体はDB側のrun_sales_judgment() SQL関数（リサーチタブの「再取得」ボタンからも
# 同じ関数を呼んでいるため、判定基準をここと二重管理しないようにしている）
def run_sales_judgment():
    conn = psycopg2.connect(**DB_CONFIG)
    cur = conn.cursor()
    cur.execute("SELECT public.run_sales_judgment()")
    updated = cur.fetchone()[0]
    conn.commit()
    cur.close()
    conn.close()
    return updated


# SKUに埋め込まれた13桁のJANを取り出す（stockdb紐付けと同じルール）
def extract_jan(sku):
    if not sku:
        return None
    m = re.search(r"\d{13}", sku)
    return m.group(0) if m else None


# サイト側の表示サイズを揃えるため、長辺がこのpxを超える画像だけ縮小する
# （縦横比は保持。元のD:ドライブ側のファイルはコピー元なので変更しない）
IMAGE_MAX_SIDE = 180


# 長辺がIMAGE_MAX_SIDEを超えていれば、縦横比を保ったまま縮小する
def resize_if_needed(path):
    with Image.open(path) as img:
        w, h = img.size
        if max(w, h) <= IMAGE_MAX_SIDE:
            return
        if w >= h:
            new_w, new_h = IMAGE_MAX_SIDE, round(h * IMAGE_MAX_SIDE / w)
        else:
            new_w, new_h = round(w * IMAGE_MAX_SIDE / h), IMAGE_MAX_SIDE
        img.convert("RGB").resize((new_w, new_h), Image.LANCZOS).save(path, "JPEG", quality=90)


# JANから商品画像を検索し、見つかればimages/フォルダへコピーする
# （既にコピー済みならスキップ。戻り値は新たにコピーしたかどうか）。
# asinを渡すと、Amazonランキング情報.pyがASIN名で保存した画像も検索先に加える
def resolve_and_copy_image(jan, asin=None):
    if not jan:
        return False
    dest_abs = os.path.join(IMAGES_DIR, f"{jan}.jpg")
    if os.path.exists(dest_abs):
        return False

    candidates = [path_fn(jan) for path_fn in IMAGE_SEARCH_PATHS]
    if asin:
        candidates.append(os.path.join(AMAZON_RANKING_IMAGE_DIR, f"{asin}.jpg"))

    for src in candidates:
        if os.path.exists(src):
            os.makedirs(IMAGES_DIR, exist_ok=True)
            shutil.copyfile(src, dest_abs)
            resize_if_needed(dest_abs)
            return True
    return False


def git_commit_and_push():
    def run(args):
        result = subprocess.run(
            args, cwd=SITE_DIR_ABS, capture_output=True,
            text=True, encoding="utf-8", errors="replace",
        )
        return result

    if os.path.isdir(IMAGES_DIR):
        run(["git", "add", "images"])
    diff = run(["git", "diff", "--cached", "--quiet"])
    if diff.returncode == 0:
        print("差分なし（新しい画像はありませんでした）")
        return

    commit = run(["git", "commit", "-m", "自動更新: 商品画像の追加"])
    if commit.returncode != 0:
        raise RuntimeError(f"commit失敗: {commit.stderr}")

    push_url = GITHUB_REPO_URL.replace("https://", f"https://{GITHUB_TOKEN}@")
    push = run(["git", "push", push_url, "HEAD:main"])
    if push.returncode != 0:
        raise RuntimeError(f"push失敗: {push.stderr}")

    print("GitHubへpushしました")


def main():
    if should_run_sales_judgment():
        updated = run_sales_judgment()
        print(f"売れ筋判定を更新しました: {updated}件")
        mark_sales_judgment_done()

    skus = fetch_all_skus()
    print(f"対象SKU件数: {len(skus)}")

    copied = 0
    for sku in skus:
        jan = extract_jan(sku)
        if resolve_and_copy_image(jan):
            copied += 1
    print(f"新たにコピーした画像件数(注文): {copied}")

    # リサーチタブに表示される商品（注文とは無関係）の画像も同期する
    research_jans = fetch_research_jans()
    jan_to_asin = fetch_jan_to_asin(research_jans)
    print(f"対象JAN件数(リサーチ): {len(research_jans)}")

    research_copied = 0
    for jan in research_jans:
        if resolve_and_copy_image(jan, asin=jan_to_asin.get(jan)):
            research_copied += 1
    print(f"新たにコピーした画像件数(リサーチ): {research_copied}")

    git_commit_and_push()


if __name__ == "__main__":
    main()
