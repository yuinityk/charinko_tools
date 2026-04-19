# charinko_tools

サイクリング向けのGPX解析・補正ツール集です。

## ツール一覧

### gpx_altitude_calibration

GPXファイルの標高データをトンネル区間で補正するWebアプリです。

**背景**  
サイクリングのGPXログには、トンネル区間でGPS信号が途切れることによる標高の大きな乱れが含まれます。このアプリでは、国土地理院（GSI）の標高APIで取得した地形標高を使い、トンネル区間を手動でアノテーションすることで補正済みGPXを出力します。

**主な機能**
- GPXルートのLeafletマップ表示（複数ファイル連結対応）
- GSI標高APIによる標高プロファイル表示
- 勾配の可視化（マップ色分け・グラフ）
- トンネル区間のアノテーション（グラフ上でドラッグ）
- アノテーションのJSON保存・読み込み・再利用
- 補正済みGPXエクスポート

**起動方法**

```bash
cd gpx_altitude_calibration
uv venv
uv pip install -r ../requirements.txt
python app.py
```

ブラウザで `http://localhost:5000` を開いてください。

**依存関係**

| パッケージ | 用途 |
|---|---|
| Flask | Webサーバー |
| aiohttp | GSI標高APIへの非同期リクエスト |

## リポジトリ構成

```
charinko_tools/
├── requirements.txt               # 共通パッケージ
├── gpx_altitude_calibration/
│   ├── app.py                     # Flaskアプリ本体
│   └── templates/
│       ├── index.html             # メイン画面
│       └── help.html              # ヘルプページ（参照用）
└── README.md
```
