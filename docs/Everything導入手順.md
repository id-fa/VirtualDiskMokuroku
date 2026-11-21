# Everything の導入手順

日本語 | [English](Everything-setup.en.md)

VirtualDiskMokuroku はファイルリストの取得に voidtools の **Everything** と、そのコマンドライン版 **ES (es.exe)** を利用します。
どちらも本アプリには同梱していないため、以下の手順で用意してください。

> **よくわからない場合は、導入しなくてもそのまま使えます。** Everything が無いときは Windows の API で
> ドライブを直接走査します(大容量ドライブでは遅くなります)。

- これから Everything を導入する方 → [1-A](#1-a-これから-everything-を導入する方)
- 既に Everything を導入している方 → [1-B](#1-b-既に-everything-を導入している方)

## 1-A. これから Everything を導入する方

1. <https://www.voidtools.com/> を開き、1.4 系 (Beta ではない方) の **「Download Portable ZIP 64-bit」** を押して
   `Everything-1.4.1.1032.x64.zip` (バージョン番号は変わります) をダウンロードします。
   - 本アプリ以外でも Everything を使いたい方は、「Download Installer 64-bit」でインストールしても構いません。
2. <https://www.voidtools.com/downloads/> (Downloads ページ。トップページには本体しか置かれていません) を開き、
   **「Download Everything Command-line Interface」** にある `ES-1.1.0.38.x64.zip` (バージョン番号は変わります) を
   ダウンロードします。
3. 2 つの ZIP を展開し、出てきたファイル (`Everything.exe`、`Everything.lng`、`es.exe`) を、VirtualDiskMokuroku の
   実行ファイルと同じ場所にある `everything_portable` フォルダに置きます。
   - このフォルダは、アプリの「ヘルプ」→「Everything の導入方法」にあるリンクからエクスプローラーで開けます。
   - インストーラー版を導入した方は `es.exe` だけを置きます。
4. スキャンを始めるときに Everything を起動するか確認されたら、「はい」を選びます。
   「設定」→「アプリ設定」の **接続テスト** ボタンで、es.exe と Everything のバージョンが表示されれば準備完了です。

## 1-B. 既に Everything を導入している方

コマンドライン版の **ES (es.exe)** は Everything 本体には含まれていないので、追加で導入します。

1. <https://www.voidtools.com/downloads/> の **「Download Everything Command-line Interface」** にある
   `ES-1.1.0.38.x64.zip` (バージョン番号は変わります) をダウンロードし、展開して `es.exe` を取り出します。
   - Everything 1.5 (アルファ版・ベータ版) を使う場合は、1.5 に対応した ES 1.1.0.30 以降を使ってください。
2. `es.exe` を [3 章](#3-esexe-の置き場所) のいずれかの場所に置きます (`everything_portable` フォルダ、
   `Everything.exe` と同じフォルダなど)。
3. 続けて 2 章のとおり Everything を起動しておき、「アプリ設定」の **接続テスト** で確認します。

## 2. Everything を起動しておく

- スキャン時には Everything が起動している必要があります(タスクトレイに常駐していれば OK)。
  - es.exe と同じフォルダ(または `everything_portable\`)に `Everything.exe` があれば、起動していないときは
    スキャン開始時と「接続テスト」で起動するか確認するダイアログが出ます。「はい」を選ぶとバックグラウンド
    (タスクトレイ常駐)で起動し、接続できるようになってからスキャンを始めます。
- NTFS ドライブのインデックス作成には **管理者権限** か **Everything サービス** が必要です。
  「ツール」→「オプション」→「全般」で「Everything サービス」を有効にしておくと、通常権限で起動してもインデックスを利用できます。
- 権限が無い場合でも本アプリは動作しますが、対象ドライブが Everything のインデックスに無いときは直接走査に切り替わります。

## 3. es.exe の置き場所

次のいずれかにしてください。

- `PATH` の通ったフォルダに置く
- VirtualDiskMokuroku の実行ファイル(またはソース)と同じフォルダ、もしくはその下の `everything_portable\` に置く
- 任意の場所に置き、アプリの「設定」→「アプリ設定」→「es.exe の場所」で指定する

「アプリ設定」の **接続テスト** ボタンで、es.exe と Everything のバージョンが表示されれば準備完了です。

### Everything 1.5 アルファ版のインスタンス名

1.5 アルファ版を既定の設定でインストールすると、インスタンス名が `1.5a` になっています。
接続テストで「Everything が起動していません」と表示される場合は、「アプリ設定」の「Everything インスタンス名」に `1.5a` を指定してください。

## 4. 外付けドライブをインデックス対象にする

Everything は既定では内蔵の NTFS ボリュームだけをインデックスします。

- **外付けの NTFS ドライブ**: 「オプション」→「インデックス」→「NTFS」で「リムーバブル ボリュームを自動的に含める」を有効にするか、対象ボリュームを個別に追加します。
- **CD/DVD、FAT/exFAT の USB メモリ・SD カードなど**: Everything のインデックス対象外のことが多いため、本アプリが自動的に直接走査へ切り替えます。特別な設定は不要です。

## 5. スキャンを速くするには (推奨設定)

本アプリは既定で「作成日時」と「属性」も取得します。Everything がこれらをインデックスしていないと、
スキャンのたびにディスクから読み直すため時間がかかります(実測: 約 115 万件のドライブで 1 秒未満 → 40〜80 秒)。

次のどちらかで解消できます。

- Everything の「オプション」→「インデックス」で **「作成日時をインデックスする」「属性をインデックスする」** を有効にする
- 本アプリの「設定」→「カタログ設定」→「一般」で **「作成日時を取得する」「属性を取得する」をオフ** にする
