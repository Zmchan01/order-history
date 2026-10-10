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


# 画像コピー対象を探すため、注文履歴にある全SKUだけを取得する
def fetch_all_skus():
    conn = psycopg2.connect(**DB_CONFIG)
    cur = conn.cursor()
    cur.execute('SELECT DISTINCT "SKU" FROM "注文履歴" WHERE "SKU" IS NOT NULL')
    rows = [r[0] for r in cur.fetchall()]
    cur.close()
    conn.close()
    return rows


# SKUに埋め込まれた13桁のJANを取り出す（stockdb紐付けと同じルール）
def extract_jan(sku):
    if not sku:
        return None
    m = re.search(r"\d{13}", sku)
    return m.group(0) if m else None


# JANから商品画像を検索し、見つかればimages/フォルダへコピーする
# （既にコピー済みならスキップ。戻り値は新たにコピーしたかどうか）
def resolve_and_copy_image(jan):
    if not jan:
        return False
    dest_abs = os.path.join(IMAGES_DIR, f"{jan}.jpg")
    if os.path.exists(dest_abs):
        return False

    for path_fn in IMAGE_SEARCH_PATHS:
        src = path_fn(jan)
        if os.path.exists(src):
            os.makedirs(IMAGES_DIR, exist_ok=True)
            shutil.copyfile(src, dest_abs)
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
    skus = fetch_all_skus()
    print(f"対象SKU件数: {len(skus)}")

    copied = 0
    for sku in skus:
        jan = extract_jan(sku)
        if resolve_and_copy_image(jan):
            copied += 1
    print(f"新たにコピーした画像件数: {copied}")

    git_commit_and_push()


if __name__ == "__main__":
    main()
