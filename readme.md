# ZC (Zabbix Clone)

# 目次
- [概要](#概要)
    - [動作環境](#動作環境)
    - [ソフトウェア要件](#ソフトウェア要件)
    - [対応するZabbixの設定](#対応するzabbixの設定)
    - [基本動作](#基本動作)
    - [用語規定](#用語規定)
- [仕様](#仕様)
    - [ノード](#ノード)
        - [マスターノード](#マスターノード)
        - [ワーカーノード](#ワーカーノード)
        - [レプリカノード](#レプリカノード)
    - [ストア](#ストア)
        - [ローカルファイル](#ローカルファイル)
        - [AWS DynamoDB](#aws-dynamodb)
        - [Redis](#redis)
        - [マスターノード直接](#マスターノード直接)
    - [実行](#実行)
        - [COMMAND](#command)
            - [clone](#clone)
            - [showversions](#showversions)
            - [showdata](#showdata)
    - [設定](#設定)
        - [設定ファイル](#設定ファイル)
            - [設定ファイルの指定](#設定ファイルの指定)
            - [設定ファイル不使用](#設定ファイル不使用)
        - [基本設定](#基本設定)
            - [ノード名](#ノード名)
            - [ロール](#ロール)
        - [接続](#接続)
            - [Zabbixエンドポイント](#zabbixエンドポイント)
            - [複製実行ユーザー](#複製実行ユーザー)
            - [複製実行ユーザーのパスワード](#複製実行ユーザーのパスワード)
            - [複製実行ユーザーのトークン](#複製実行ユーザーのトークン)
            - [HTTP認証](#http認証)
            - [自己証明書の利用](#自己証明書の利用)
        - [実行設定](#実行設定)
            - [パスワード変更](#パスワード変更)
            - [強制初期化](#強制初期化)
            - [IPアドレス利用の強制](#ipアドレス利用の強制)
            - [ホスト設定の強制更新](#ホスト設定の強制更新)
            - [既存設定削除の不実行](#既存設定削除の不実行)
            - [テンプレートのスキップ](#テンプレートのスキップ)
            - [テンプレートの分離数](#テンプレートの分離数)
            - [CheckNowの実行](#checknowの実行)
            - [CheckNowを実行する監視間隔](#checknowを実行する監視間隔)
            - [ホスト適用の並列実行数](#ホスト適用の並列実行数)
        - [ストア設定](#ストア設定)
            - [ストアの指定](#ストアの指定)
            - [AWS DynamoDBの接続設定](#aws-dynamodbの接続設定)
            - [Redisの接続設定](#redisの接続設定)
            - [マスターノード直接の接続設定](#マスターノード直接の接続設定)
        - [Zabbix追加設定](#zabbix追加設定)
            - [暗号化グローバルマクロ](#暗号化グローバルマクロ)
            - [プロキシーの通信暗号化](#プロキシーの通信暗号化)
            - [一般設定](#一般設定)
            - [複製許可ユーザー](#複製許可ユーザー)
            - [通知メディア設定](#通知メディア設定)
            - [MFAシークレット](#mfaシークレット)
        - [データベース設定](#データベース設定)
    - [動作概要](#動作概要)
    - [テスト状況](#テスト状況)
    - [注意事項](#注意事項)
    - [現在未対応](#現在未対応)
    - [要確認](#要確認)
    - [対応予定なし](#対応予定なし)
    - [機能追加したいもの](#機能追加したいもの)
- [FAQ](#faq)
- [免責事項](#免責事項)

<hr>

# 概要

(Will be translated into English.)

Zabbixの監視設定をAPIを使って設定元のZabbixから、監視を実行するZabbixに複製するツールです。完全なバックアップではありません。

とりあえず7.0->7.0で動くようになったので公開します。

## 動作環境
Pythonが動作するOSなら多分どれでも。

動作確認はWindows11pro、CentOS7で実行しています。

## ソフトウェア要件

* Zabbix 4.0 Later
* Python 3.9 Later

Zabbixのバージョンは4.0以降（開発系は除外）に対応します。

必要なPythonライブラリ

* pyzabbix
* redis
* boto3

Zabbix6.0より前のバージョンはデータベース操作が必要になるので以下のライブラリも必要になります。

* PostgreSQL: psycopg2
* MySQL/MariaDB: pymysql

## 対応するZabbixの設定

* ホストグループ
* テンプレートグループ
* ホスト
* テンプレート
* アクション
* スクリプト
* メンテナンス
* ネットワークディスカバリ
* サービス（6.0以降）
* SLA
* イベント相関
* ユーザー
* ユーザーのメディア利用設定
* ユーザーグループ
* ロール
* 一般設定
* 暗号化グローバルマクロ
* プロキシ
* プロキシグループ
* 認証（動作未確認）
* LDAP認証設定（動作未確認）
* SAML認証設定（動作未確認）
* MFA認証設定（動作未確認）


## 基本動作

設定の複製に必要な動作は以下の２つになります。

* マスターノードに対してコマンドを実行しデータストアに監視設定を保存
* ワーカーノードに対してコマンドを実行し保存した監視設定を適用

Zabbix APIのエンドポイントに対して通信を行うため、それぞれのZabbixを動作させているマシンではない端末、例えばクライアントPC上から実行が可能です。ただしZabbix6.0より前のバージョンではデータベース操作が必要になるため、アクセス制限の関係でそれぞれのZabbix Server上で実行する必要があるかもしれません。

ストアはローカルファイル / AWS DynamoDB / Redis / マスターノード直接 の４種類に対応し、デフォルトの動作はローカルファイルになります。ローカルファイルは本ツールを実行するコンピュータのローカルにバージョンファイルを作成し、利用します。詳細は「[ストア](#ストア)」の項で説明します。

動作設定は設定ファイルとコマンドライン引数を利用します。ただしZabbix内部設定に関わる設定は設定ファイルでの対応のみになります。設定ファイルは以下の場所に設置してある場合、指定なしに読み込まれます。

* /etc/zabbix/zc.conf
* /var/lib/zabbix/conf.d/zc.conf

設定ファイルを指定する場合はコマンドライン引数で以下のように設定します。

```sh
zc.py clone --config-file ./file
```

ファイルにパスが通るなら相対パスでも絶対パスどちらでも可能です。
設定ファイルを指定した場合、上記の固定のファイルは読み込みません。

コマンドライン変数は以下の引数でヘルプが表示されます。

```sh
zc.py --help
```

設定ファイルはJSONで記述します。
```config.json
{
    "node": "monitoring node name",
    "role": "master|woker",
    "endpoint": "http://localhost:8080/",
    "user": "zabbix admin username",
    "password":"zabbix admin password",
    "update_password": "YES|NO default:NO"
    "token": "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
    "http_auth": "YES|NO default:NO",
    "self_cert": "YES|NO default:NO",
    "checknow_execute": "YES|NO default:NO",
    "store_type": "file|redis|dydb|direct,
    "store_connect": {
        "aws_access_id": "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
        "aws_secret_key": "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
        "aws_region": "us-east-1",
        "dydb_limit": 100,
        "redis_host": "zc-master",
        "redis_port": 6379,
        "redis_password": "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
        "direct_endpoint": "http://master.node.endpoint/",
        "direct_token": "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxOnlyTokenAuth"
    }
}
```
詳細は「[設定](#設定)」の項で行います。

### 用語規定

#### ホスト
これ以降の説明において、「ホスト」は全てZabbix設定のホストを意味します。
例外として「データベースホスト」のみ、データベースの接続先を意味します。

#### エンドポイント
これ以降の説明において、「エンドポイント」は各機能の接続先を意味します。
例外として「データベースホスト」のみ、データベースの接続先を意味します。

# 仕様

## ノード

ZabbixCloneでは、Zabbixサーバーをノードと呼んでいます。
元は設定マスターのZabbixから監視実行Zabbixへ設定を複製しクラスター動作させることを目的としています。

### マスターノード
    設定マスターZabbix / role: master
    全てのZabbix設定のマスターとなるZabbix。
    「ZC_UUID: UUID」のタグで、ホストのユニークを管理する。このタグはホストに存在しなければマスターノードの複製作成時に自動的に設定される。
    ホスト名変えてもZC_UUIDが変わらなければ同じホスト扱いとなる。
    テンプレート、ホストグループ、トリガーに既にあるパラメーターなため、Zabbixが同様にホストにも適用した場合はオミットされる。
    基本的にすべての設定をマスターノードからAPIで取得するが、シークレット情報はZabbix APIは出力しないため設定ファイルに記述する。
    マスターノードでは監視無効にしてあるホストは、ワーカーノードで設定する際に有効になる。

### ワーカーノード
    監視実行Zabbix / role: worker
    監視を実行するZabbixで、マスターノードと同じかそれより新しいバージョンのZabbixでなければならない。
    適用する設定のバージョンを指定しない場合、ストアにある最新のデータでが適用される。
    マスターノード側でホストにタグ（ZC_WORKER:ノード名）で動作するワーカーノードを指定し、そのホストのみを対象のワーカーノードに複製する。
    ホストの複製時、自動的に監視開始状態にする。
    ホスト以外の設定は基本的に全て適用される。
    ワーカーノードのに存在しない設定は生成、存在する設定は更新される。
    マスターノードの設定に無く、ワーカーノードに存在する設定は削除される。

### レプリカノード
    マスターノードの複製 / role: replica
    ワーカーノードとの違いは全ホストを複製するが、監視有効にしない。
    ユーザーに設定される通知設定は複製されない。

## ストア
ローカルファイル(file)  / AWS DynamoDB（dydb） / Redis（redis） / マスターノード直接（direct）
デフォルトはローカルファイル

基本的にマスターノードの設定はストアに保存します。
設定は取得した際にUUIDのバージョン番号が付与されます。
マスターノードから直接設定を取得し、ワーカーノードに適用することも可能ですが、その際にはバージョン管理はできません。

### ローカルファイル
    store type: file

    ディレクトリ:
        Linux: /var/lib/zabbix/zc
        Windows: ユーザープロファイル\マイドキュメント\zc
    
    ファイル名フォーマット:
        バージョンUUID_タイムスタンプ_マスターノードZabbixバージョン.bz2

    ・バージョン指定は「UUIDのバージョン番号」を利用する
    ・バージョン指定がない場合は作成タイムスタンプが最新のものを利用する
    ・ファイルの場所は指定できない
    ・ディレクトリを自動作成はしない

### AWS DynamoDB
    store type: dydb
    
    次の２つのテーブルが必要、自動的に作成はしない
    テーブルはそれぞれ以下の設定

    ZC_VERSION バージョン情報
        VERSION_ID      (S) Partition Key
        UNIXTIME        (N) Sort Key
        MASTER_VERSION  (S) マスターノードのZabbixバージョン
        DESCRIPTION     (S) 補足情報

    ZC_DATA    Zabbixデータ
        VERSION_ID      (S) Partition Key
        DATA_ID         (S) Sort key
        METHOD          (S) Zabbixメソッド
        NAME            (S) メソッド内のユニーク名称
        DATA            (B) 内容のJSON出力 -> bz2圧縮

### Redis
    store type: redis
    
    DBを２つ利用
    パスワード利用可能

    db:0    バージョン情報  hash
        VERSION_ID: {
            'UNIXTIME': b'1234567890',
            'MASTER_VERSION': b'x.y',
            'DESACRIPTION': b'補足情報'
        }
    db:1    Zabbixデータ    hash
        VERSION_ID: {
            b'DATA_ID': b'内容のJSON出力 -> bz2圧縮',
            ...
        }

### マスターノード直接
    store type: direct
    
    マスターノード直接はマスターノードの現在の設定を直接ワーカーノードに適用するのでバージョン管理はできない
    マスターノードでの作業はなく、ワーカーノード設定だけで実行する
    マスターノードへの認証はトークンのみを利用できる

## 実行

### COMMAND

    clone : 複製の実行
    showversions: ストアに保存されているバージョンの確認
    showdata: ストアに保存されている対象バージョンのデータ確認
    delete: 対象バージョンの特定データを削除（未実装）
    clearstore: ストア内のデータをすべて削除（未実装）


#### clone
```sh
# マスターノードからの設定取得実行
zc.py clone --role master --node master-zabbix --store-type file --endpoint http://master-zabbix.example.com/ --user Admin --password zabbix
```

```sh
# ワーカーノードへの設定複製実行
zc.py clone --role worker --node worker-zabbix --store-type file --endpoint http://worker-zabbix.example.com/ --user Admin --password zabbix
```
##### option
    --role {master,worker,replica}
    対象Zabbixの役割

    --node value
    対象Zabbixのサーバー名

    --store-type {file,dydb,redis,direct}
    default: file
    データ保存方法の指定

    --force-initialize
    対象のワーカーノードの強制初期化

    --template-skip
    テンプレートのインポートを実行しない

    --no-delete
    ワーカーノードに既にある設定を消さない
    強制初期化が優先される

    --checknow-execute
    default: 1h
    ホスト設定適用後、LLDすべてと指定監視間隔のアイテムの値取得を実行する

#### showversions
```sh
# バージョンの確認
zc.py showversions
```

##### option
    --id-only
    バージョンIDとタイムスタンプ（UNIXTIME）のみ表示

#### showdata
```sh
# 保存データの確認
zc.py showdata --version xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
```
##### required
    --version value, -v value
    value: バージョンID
    
##### option
    --id-only
    バージョンIDとタイムスタンプ（UNIXTIME）のみ表示

    --method value [value ...]
    指定のメソッドのみ表示

    --name value [value ...]
    指定の名称のみ表示


## 設定

設定は、固定設定ファイルまたは指定されたファイルが読み込まれた後、コマンドパラメーターが適用されて決定します。

設定のCOMMANDが有効なのは[clone](#clone)のみになります。

### 設定ファイル

#### 設定ファイルの指定
    COMMAND: --config-file value, -f value
    value: パス付の任意の設定ファイル

#### 設定ファイル不使用
    COMMAND: --no-config-files

    コマンド引数で指定した場合、設定ファイルを読み込まない

### 基本設定

#### ノード名
    COMMAND: --node value, -n value
    CONFIG: {"node": "value"}
    value: 任意のZabbixサーバー名
    
    ログインページ内の「<div class="server-name>node">value</div>」を確認する
    適用時、次の機能は複製データの内容を確認しノード名が同じなら実行する
        ホスト: タグ「ZC_WORKER」の値
        プロキシー: ディスクリプションに記述された「ZC_WORKER:ノード名;」

#### ロール
    COMMAND: --role value, -r value
    CONFIG: {"role": "value"}
    value:
        master: 設定マスター
        worker: 監視実行
        develop: 監視不実行の設定コピー
    default: worker
    
### 接続

#### Zabbixエンドポイント
    COMMAND: --endpoint value, -e value
    CONFIG: {"endpoint": "value"}
    value: ZabbixのURL
    default: http://localhost:8080/

    操作対象のZabbixエンドポイント指定
    api_jsonrpc.phpの記述は不要

#### 複製実行ユーザー
    COMMAND: --user value, -u value
    CONFIG: {"user": "value"}
    value: 任意のZabbixユーザー
    default: Admin

    Zabbixへパスワード認証する場合のユーザー名
    デフォルトはZabbix初期値

#### 複製実行ユーザーのパスワード
    COMMAND: --password value, -p value
    CONFIG: {"password": "value"}
    value: 任意のパスワード
    default: zabbix

    Zabbixへパスワード認証する場合のパスワード
    また変更用のパスワード
    デフォルトはZabbix初期値

#### 複製実行ユーザーのトークン
    COMMAND: --token value, -t value
    CONFIG: {"token": "value"}
    value: 任意のトークン/セッションID

    Zabbixへトークン認証する場合のトークン
    パスワードとトークン認証が設定されている場合、トークン認証が優先される
    Zabbix5.4以前はトークン機能が存在しないが、クッキーのセッションIDが同じように利用できる

#### HTTP認証
    COMMAND: --http-auth
    CONFIG: {"http_auth": "YES|NO"}
    default: "NO"

    HTTP認証をZabbixで利用する
    トークン認証が無効化される

#### 自己証明書の利用
    COMMAND: --self-cert
    CONFIG: {"self_cert": "YES|NO"}
    default: "NO"

    自己証明書を利用している場合、確認をスキップする

### 実行設定

#### パスワード変更
    COMMAND: --update-password
    CONFIG: {"update_password": "YES|NO"}
    default: "NO"

    複製実行ユーザーのパスワードに設定されている値で接続ユーザーのパスワードを更新する
    有効になっている場合、トークン認証またはZabbixの初期認証情報を利用する

#### 強制初期化
    COMMAND: --force-initialize
    CONFIG: {"force_initialize": "YES|NO"}
    default: "NO"

    対象の全設定を削除する
    ZabbixCloneで複製されていない対象の場合、この設定がなくても強制的に初期化される

#### IPアドレス利用の強制
    COMMAND: --force-userip
    CONFIG: {"force_useip": "YES|NO"}
    default: "NO"

    ホストの接続設定を強制的にIPアドレスに変更する
    対象のワーカーノードでFQDNからIPアドレスに変換できない場合はFQDNから変更しない

#### ホスト設定の強制更新
    COMMAND: --force-host-update
    CONFIG: {"force_host_update": "YES|NO"}
    default: "NO"

    ホストのホスト名が同じだがZC_UUIDが違う場合は更新しないが、これを有効にした場合は上書き更新を行う

#### 既存設定削除の不実行
    COMMAND: --no-delete
    CONFIG: {"no_delete": "YES|NO"}
    default: "NO"
    
    各監視対象が監視マスターのデータにないものは削除されるが、これを有効にした場合は削除しない

#### テンプレートのスキップ
    COMMAND: --template-skip
    CONFIG: {"template_skip": "YES|NO"}
    default: "NO"

    マスターノード側でテンプレートのエクスポートを実行しない
    ワーカーノード側でテンプレートのインポートを実行しない

#### テンプレートの分離数
    COMMAND: --template-separate integer
    CONFIG: {"template_separate": integer}

    マスターノード側でテンプレートのエクスポートを分離数ごとに実行する
    ワーカーノード側での処理はない

#### CheckNowの実行
    COMMAND: --checknow-execute
    CONFIG: {"checknow_execute": "YES|NO"}
    default: "NO"

    これを有効にした場合、ホストをワーカーノードに適用後に全LLDと任意の監視間隔のアイテムの値取得を実行する
    依存アイテムの場合、親アイテムも実行する

#### CheckNowを実行する監視間隔
    COMMAND: --checknow-interval value [value ...]
    CONFIG: {"checknow_interval": ["value", "value", ...]}
    default: 1h

    アイテムの値取得を実行する対象の監視間隔の指定
    タイムサフィックス(m, h, d)は秒に展開される

#### ホスト適用の並列実行数
    COMMAND: --php-worker-num integer
    CONFIG: {"php_worker_num": integer}
    default: 4

    ホストのワーカーノードへの適用を並列実行する数
    php-fpmでこれを変更する場合、プロセス数を合わせて変更しなければ実行速度が落ちる可能性がある

### ストア設定

#### ストアの指定
    COMMAND: --store-type value, -s value
    CONFIG: {"store_type": "value"}
    value: 
        file: ローカルファイル
        dydb: AWS DynamoDB
        redis: Redis
        direct: マスターノード直接
    default: file

    Zabbix設定の保存先指定

#### AWS DynamoDBの接続設定

##### AWS Account IDの指定

    COMMAND: --store-access value, -sa value
    CONFIG: {"store_connect": {"aws_ccount_id": "value"}}
    value: AWS Account ID

    .aws/credentialを利用しない場合に設定する

##### AWS Secret Keyの指定

    COMMAND: --store-credential value, -sc value
    CONFIG: {"store_connect": {"aws_secret_key": "value"}}
    value: AWS Secret Key

    .aws/credentialを利用しない場合に設定する

##### AWS Regionの指定
    COMMAND: --store-endpoint value, -se value
    CONFIG: {"store_connect": {"aws_region": "value"}}
    value: AWS Secret Key
    default: us-east-1

    .aws/credentialを利用しない場合に設定する

##### 操作レコードの制限数
    COMMAND: --store-limit integer
    CONFIG: {"store_connect": {"dydb_limit": integer}}
    default: 100

    DymanoDBの負荷制御のパラメータ―、制限数
    制限数ごとに待機秒数のインターバルを挟む

##### バッチ操作待機秒数
    COMMAND: --store-interval integer
    CONFIG: {"store_connect": {"dydb_wait": integer}}
    default: 1

    DymanoDBの負荷制御のパラメータ―、待機秒数
    制限数ごとに待機秒数のインターバルを挟む

#### Redisの接続設定

##### Redisのエンドポイント
    COMMAND: --store-endpoint value, -se value
    CONFIG: {"store_connect": {"redis_host": "value"}}
    value: IP/FQDN
    default: localhost

    Redisの接続先指定 

##### Reidsのポート
    COMMAND: --store-port value, -sp value
    CONFIG: {"store_connect": {"redis_port": value}}
    value: integer
    default: 6379

    Redisの接続先ポート指定

##### Reidsのパスワード
    COMMAND: --store-credential value, -sc value
    CONFIG: {"store_connect": {"redis_password": "value"}}
    value: 任意のパスワード

    Redisの接続パスワード

#### マスターノード直接の接続設定

##### マスターノード名
    COMMAND: --store-access value, -sa value
    CONFIG: {"store_connect": {"direct_node": "value"}}
    value: マスターノードのZabbixサーバー名

    マスターノードの接続先のZabbixサーバー名
    接続時に確認する

##### マスターノードエンドポイント
    COMMAND: --store-endpoint value, -se value
    CONFIG: {"store_connect": {"direct_endpoint": "value"}}
    value: マスターノードZabbix URL

    マスターノードのZabbixエンドポイント指定
    api_jsonrpc.phpの記述は不要

##### マスターノードトークン
    COMMAND: --store-credential value, -sc value
    CONFIG: {"store_connect": {"direct_token": "value"}}
    value: マスターノードのトークン

    マスターノードの特権管理者権限トークン

### Zabbix追加設定

#### 暗号化グローバルマクロ
    CONFIG: {"secret_globalmacro": [value, value, ...]}
    value: {"macro": "macro_name", "value": "macro_value"}

    暗号化マクロの値はAPIで取得できないため、設定ファイルに記述した暗号化グローバルマクロmacro_nameにmacro_valueを設定する

#### プロキシーの通信暗号化
    CONFIG: {"proxy_psk": {"proxy": ["psk_identity", "psk"]}}

    pskはAPIで取得できないため、設定ファイルに記述したものを設定する

#### 一般設定
一般設定の内、Zabbix7.0以降対応のデータ収集のタイムアウト設定と重要度名称を設定ファイルから設定します。
ワーカーノード側でマスターノードと違う設定を可能にします。

##### データ収集のタイムアウト設定
    CONFIG: {"settings": {"timeout": {"target": "value"}}}
    target:
        zabbix_agent
        simple_check
        snmp_agent
        external_check
        db_monitor
        http_agent
        ssh_agent
        telnet_agent
        script
        browser
    value: 1s-(600s|10m)
    default:
        external_check: 15s

    targetのタイムアウト設定を1秒から600秒の範囲で指定する
    external_checkのみ、初期設定の5sではタイムアウト発生でZabbix Serverの不正終了が頻発するためデフォルト15sを設定（7.0.2での確認）

##### 重要度名称の設定
    CONFIG: {"settings": {"severity": {value, value, ...}}}
    value: {"level": {"name": "severity_name", "color": "hex_color"}}

    levelは0-5の重要度レベル

#### 複製許可ユーザー
    CONFIG: {"enable_user": value}
    value: {"user": "password"}

    マスターノードに設定されているuserをワーカーノードに複製する
    パスワードはAPIで取得できないため、設定ファイルに記述したものを設定する

#### 通知メディア設定
ワーカーノードごとに通知先設定の変更を可能にする設定です。

    CONFIG: {"media_settings": {"user": value}}
    user: マスターノードに設定されている、かつ複製許可ユーザーに設定されている
    value: 
        {"to": [address, address, ...]}
        {"severity": severity}
        {"work_time": work_time}
    required: to, severity, work_time

##### severity

    CONFIG: {"severity": value}
    value: {"level": "YES|NO"}
    level: 0-5

##### work_time

    CONFIG: {"week_day": "HH:MM-HH:MM"}
    weed_day: Mon / Tue / Wed / Thu / Fri / Sat / Sun

##### MFAシークレット

    CONFIG: {"mfa_client_secret": value}
    value: {"name": "secret"}

    Duoユニバーサルプロンプトで「name（名前）」に対応するクライアントシークレット

### データベース設定
Zabbix6.0より前のバージョンでは一般設定のAPIがないためデータベース操作で直接取得します。
Zabbix Server上で実行した場合、/etc/zabbix/zabbix_server.confより取得します。
取得できない端末から実行する場合はこのパラメーターを設定してください。

#### データベースホスト
    COMMAND: --db-connect-host value, -dbhost value
    CONFIG: {"db_connect": {"host": "value"}}
    value: データベースのFQDN/IPアドレス
    default: localhost

#### データベース名
    COMMAND: --db-connect-name value, -dbname value
    CONFIG: {"db_connect": {"name": "value"}}
    value: Zabbixデータベース名
    default: zabbix

#### データベース種別
    COMMAND: --db-connect-type value, -dbtype value
    CONFIG: {"db_connect": {"type": "mysql|pgsql"}}
    value: データベースの種別
    default: pgsql

    MySQL系、PostgreSQL系のみの対応
    ポート番号の指定はない

#### データベース接続ユーザー
    COMMAND: --db-connect-user value, -dbuser value
    CONFIG: {"db_connect": {"user": "value"}}
    value: Zabbixデータベースへの接続ユーザー
    default: zabbix

#### データベース接続パスワード
    COMMAND: --db-connect-password value, -dbpswd value
    CONFIG: {"db_connect": {"password": "value"}}
    value: Zabbixデータベース接続ユーザーのパスワード
    default: zabbix

## 動作概要
### マスターノード動作
<img src="./images/master-node_processing.png" width="50%">

### ワーカーノード動作
<img src="./images/worker-node_processing.png" width="50%">

## テスト状況
基本的にLTSから1つ上のLTS、LTSから同じバージョン台のPoint Rleaseをテストします。
それ以外はパターンが多すぎるのでできませんので、誰か試したら結果教えてください。

|master|worker|ZabbixCloud|file|dydb|redis|direct|
|:-:|:-:|:-:|:-:|:-:|:-:|:-:|
|7.0|7.0||OK||OK||
|6.0|7.0|*1|||||
|6.0|6.4|N/A|||||
|6.0|6.2|N/A|||||
|6.0|6.0|N/A|||||
|5.0|6.0|N/A|?|?|?|?|
|5.0|5.4|N/A|||||
|5.0|5.2|N/A|||||
|5.0|5.0|N/A|||||
|4.0|5.0|N/A|||||
|4.0|4.4|N/A|||||
|4.0|4.2|N/A|||||
|4.0|4.0|N/A|||||

*1: ZabbixCloudが7.0の間のみ

## 注意事項
2024/11月現在
* super admin roleでの実行、Adminを想定
* usergroupの「Zabbix administrators」の名前は変更してはいけない
* 5.4で書式変換が必要になるけど、そのあたりまだテストしてない（configuration.importが処理してくれる？）
* Zabbixのバージョンはマスターノード≦ワーカーノードで動作
* バックアップ機能ではないので、自動登録されたものは複製できない
    自動登録（LLD、ディスカバリ、自動登録アクション）のホスト／アイテム／トリガーはワーカーノードでそれぞれ実行されて登録される
* 監視設定はテンプレートのみ（configuration.export/importを利用）
    ホストに直接設定されたアイテム/トリガーは破棄される（Zabbixのバージョンで使える使えないの判定、トリガー書式の変換処理とかやってられない）
* ホストはhost.create|updateの実行
    けれどconfiguration.importでホストを処理しないのはホスト数が多いとタイムアウトで失敗するため
    （httpd/nginxのtimeout設定にもよるけどデフォルトだと100ちょっとくらい、phpのメモリ設定も無駄にでかくなる）
    1ホストずつのAPI実行でタイムアウトを回避、並列実行で実行時間の短縮（4並列でだいたい半分くらい）
* configuration.importの項目チェック機能が厳密化実装以降わりとバグるのでホストの処理にまでその対応を入れたくない
    不要になった項目吐いてるのに食わせるとエラーになるとか
* トリガーアクションでトリガー指定は複製できない（複製の指定（ホスト＆トリガー、自動登録も考慮）が非常に面倒なので）
* 複数のバージョンの順次適用はない
    途中のバージョンの適用が必要という状況は今のところ想定していない
* Zabbix5.4より前はトークンのシステムがないが、セッションIDが同じものとして使える

## 現在未対応
* レポート（ダッシュボードの指定が必要なため）
* コネクタ（7.0機能、まだ使ったことがないのでよくわからない）

## 要確認
* ネットワークディスカバリ（まだプロキシグループに対応していないので、7.2以降変更ありそう）

## 対応予定なし
主にUI関連
*  Zabbix 1.x/2.x/3.x
* ダッシュボード
* アイコン
* イメージ
* トークン

## 機能追加したいもの
* git対応
* 公式テンプレートのダイレクトインポート
* ワーカーノード側の実行前バックアップ
* 失敗時の戻し

# FAQ
* 質問がたまったら作る、質問来るほど使われない気もする。

# 免責事項

このスクリプトはMITライセンスを採用しています。
スクリプトを実行した結果の責任は一切負いません。
