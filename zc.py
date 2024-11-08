#!/usr/bin/env python3
# -*- coding: utf-8 -*-
'''
Zabbix Clone: Zabbix monitoring settings cloning tool, from master-Zabbix to worker-Zabbix.

Copyright (c) 2024 tsuno teppei
Released under the MIT license
https://opensource.org/licenses/mit-license.php
'''
__author__ = 'tsuno.teppei'
__version__ = '0.1.0'
__date__ = '2024/10/23'

import os
import sys
import json
import uuid
import requests
import boto3
from boto3.dynamodb.conditions import Key
from pyzabbix import ZabbixAPI
import re
import bz2
import socket
from datetime import datetime, UTC
from calendar import timegm
from time import sleep
import redis
from concurrent import futures
import inspect
import argparse
import shutil
import textwrap

# ZABBIX関連の固定値とか
ZABBIX_DEFAULT_AUTH = ['Admin', 'zabbix']
ZABBIX_CONFIG_PATH = '/etc/zabbix'
ZABBIX_SERVER_CONFIG = 'zabbix_server.conf'
ZABBIX_USER_CONFIG_PATH = '/var/lib/zabbix/conf.d'
ZABBIX_SNMP_COMMUNITY = '{$SNMP_COMMUNITY}'
ZABBIX_TEMPLATE_ROOT = 'Templates'
ZABBIX_ENABLE = '0'
ZABBIX_DISABLE = '1'
ZABBIX_SUPER_USER = 'Admin'
ZABBIX_SUPER_GROUP = 'Zabbix administrators'
ZABBIX_SUPER_ROLE = 3
ZABBIX_WEEKDAY = {'MON': 1, 'TUE': 2, 'WED': 3, 'THU': 4, 'FRI': 5, 'SAT': 6, 'SUN': 7}
ZABBIX_INVENTORY_MODE = {'DISABLED': -1, 'MANUAL': 0, 'AOTOMATIC': 1}
ZABBIX_IFTYPE = {'AGENT': 1, 'SNMP': 2, 'IPMI': 3, 'JMX': 4, 1: 'AGENT', 2: 'SNMP', 3: 'IPMI', 4: 'JMX'}
ZABBIX_SNMP_VERSION = {'SNMPV1': 1, 'SNMPV2': 2, 'SNMPV3': 3}
ZABBIX_PROXY_MODE = {'direct': 0, 'proxy': 1, 'proxy_group': 2}

# 並行処理同時実行数のデフォルト値
PHP_WORKER_NUM = 4

# ZabbixCloneパラメーター
ZC_DEFAULT_ZABBIX_VERSION = 7.0
ZC_DEFAULT_NODE = 'zc-default'
ZC_DERAULT_ROLE = 'worker'
ZC_HEAD = 'ZC_'
ZC_UNIQUE_TAG = ZC_HEAD + 'UUID'
ZC_CONFIG = 'zc.conf'
ZC_MAINTE_NAME = '__ZC_UPDATE__'
ZC_MONITOR_TAG = ZC_HEAD + 'WORKER'
ZC_NOTICE = 'Email'
ZC_NOTICE_USER = ZABBIX_SUPER_USER
ZC_NOTICE_TO = 'alert@example.com'
ZC_DEFAULT_STORE = 'file'
ZC_ALL_ALLOW_ROLE = ['master', 'replica']
ZC_NO_NOTICE_ROLE = ['replica']
ZC_COMPLETE = (True, 'Complete.')
ZC_TEMPLATE_SEPARATE = 100
ZC_NODE_ID = 'ZC_NODE_ID'
ZC_FILE_STORE = ['/var/lib/zabbix', 'Documents']
ZC_VERSION_CODE = '{$ZC_VERSION}'

# 表示系
SIZE = shutil.get_terminal_size()
WIDE_COUNT = SIZE.columns
LINE_COUNT = SIZE.lines
T_CHAR = ' '
T_COUNT = 4
TAB = T_CHAR * T_COUNT
B_CHAR = '-'
B_COUNT = WIDE_COUNT - T_COUNT
BD = B_CHAR * B_COUNT

# 6.0以降Settingsデフォルト値
## 7.0 のタイムアウト対応、ExternalCheckがタイムアウトするとZabbixが死ぬので注意（7.0.2で確認）
ZC_TIMEOUT_LOWER = {
    'external_check': 15,
}

# アラート通知デフォルト
ZC_DEFAILT_ALERT = {
    'user': [
        [ZC_NOTICE_USER, ZC_NOTICE_TO]
    ],
    'severity': {
        0:'YES',
        1:'YES',
        2:'YES',
        3:'YES',
        4:'YES',
        5:'YES'
    },
    'worktime': {
        "Mon": "00:00-24:00",
        "Tue": "00:00-24:00", 
        "Wed": "00:00-24:00", 
        "Thu": "00:00-24:00", 
        "Fri": "00:00-24:00", 
        "Sat": "00:00-24:00", 
        "Sun": "00:00-24:00"
    }
}

# 実行時のunixtime生成（UTC）
def UNIXTIME():
    return int(timegm(datetime.now(UTC).timetuple()))

# 実行時のunixtimeをZABBIXの時刻フォーマットに変換（UTC）
def ZABBIX_TIME():
    return datetime.now(UTC).strftime('%Y-%m-%dT%H:%M:%SZ')

# リスト１のすべてがリスト２にあるか確認
def LISTA_ALL_IN_LISTB(listA=[], listB=[]):
    if not isinstance(listA, list) or not isinstance(listB, list):
        return False
    return all(map(listB.__contains__, listA))

# ノード名の確認
def CHECK_ZABBIX_SERVER_NAME(endpoint, name):
    prefix = '<div class="server-name">'
    suffix = '</div>'
    res = requests.get(endpoint)
    if not res.ok:
        return (False, 'Cannot Get ServerName.')
    res = re.findall(f'{prefix}[a-zA-Z0-9-]*{suffix}', res.text)
    if not res:
        return (False, 'Not Find ServerName.')
    res = res[0].removeprefix(prefix)
    res = res.removesuffix(suffix)
    if res != name:
        return (False, f'Wrong Target Node {name}.')
    return ZC_COMPLETE

class ZabbixCloneConfig():
    '''
    ZabbixClone設定クラス
    ストア種別がdirectの時はマスター側の接続設定として扱う
    '''

    def __init__(self, **params):
        self.result = None
        self.directMaster = False
        self.configFile = None
        self.result = self.readConfig(**params)

    def readConfig(self, **params):
        '''
        設定ファイルの初期化
        1.指定された設定ファイルまたはデフォルト設定ファイル読み込み
        2.ノード設定ファイル読み込み
        3.引数の処理
        4.内容確認＆デフォルト処理
        '''
        # no_config_files: YESの場合は引数のみを使用
        CONFIG = {}
        if params.get('no_config_files', 'NO') == 'YES':
            pass
        else:
            # 指定された設定ファイルまたはデフォルト設定ファイル
            self.configFile = params.get('config_file', os.path.join(ZABBIX_CONFIG_PATH, ZC_CONFIG))
            if os.path.exists(self.configFile) and os.access(self.configFile, os.R_OK):
                # 基本設定ファイル読み込み
                try:
                    with open(self.configFile, 'r') as f:
                        CONFIG = json.load(f)
                except Exception as e:
                    print(e)
                    pass

            # 引数でのファイル指定なし
            if not params.get('config_file'):
                # ユーザー設定読み込み/上書き
                nodeConfig = os.path.join(ZABBIX_USER_CONFIG_PATH, ZC_CONFIG)
                if os.path.exists(nodeConfig) and os.access(nodeConfig, os.R_OK):
                    try:
                        userConf = json.load(f)
                        with open(nodeConfig, 'r') as f:
                            for param, value in userConf.items():
                                if param in CONFIG:
                                    CONFIG.update({param: value})
                    except:
                        pass

        # 引数読み込み・上書き
        for param, value in params.items():
            CONFIG.update({param: value})

        # クラス変数化
        # ノード名
        self.node = CONFIG.get('node', ZC_DEFAULT_NODE)
        # ストア種別
        self.storeType = CONFIG.get('store_type', ZC_DEFAULT_STORE)
        if self.storeType == 'extend':
            self.storeType = CONFIG.get('extend_store', ZC_DEFAULT_STORE)
        # ストア接続情報
        self.storeConnect = CONFIG.get('store_connect', {})
        if self.storeType == 'dydb':
            self.storeConnect.update(
                {
                    'awsAccessId': CONFIG.get(
                        'store_access',
                        self.storeConnect.get('aws_account_id', None)
                    ),
                    'awsSecretKey': CONFIG.get(
                        'store_credential',
                        self.storeConnect.get('aws_secret_key', None)
                    ),
                    'awsRegion': CONFIG.get(
                        'store_endpoint',
                        self.storeConnect.get('aws_region', 'us-east-1')
                    ),
                    'dydbLimit': CONFIG.get(
                        'store_limit',
                        self.storeConnect.get('dydb_limit', 10)
                    ),
                    'dydbWait': CONFIG.get(
                        'store_interval',
                        self.storeConnect.get('dydb_wait', 2)
                    ),
                }
            )
        elif self.storeType == 'redis':
            self.storeConnect.update(
                {
                    'redisHost': CONFIG.get(
                        'store_endpoint',
                        self.storeConnect.get('redis_host', 'localhost')
                    ),
                    'redisPort': CONFIG.get(
                        'store_port',
                        self.storeConnect.get('redis_port', 6379)
                    ),
                    'redisPassword': CONFIG.get(
                        'store_credential',
                        self.storeConnect.get('redis_password', None)
                    )
                }
            )
        elif self.storeType == 'direct':
            self.storeConnect.update(
                {
                    'directNode': CONFIG.get(
                        'store_access',
                        self.storeConnect.get('direct_node', None)
                    ),
                    'directEndpoint': CONFIG.get(
                        'store_endpoint',
                        self.storeConnect.get('direct_endpoint', None)
                    ),
                    'directToken': CONFIG.get(
                        'store_credential',
                        self.storeConnect.get('direct_token', None)
                    ),
                }
            )
        else:
            # Extendストアのパラメーター
            try:
                self.storeConnect = json.loads(CONFIG.get('extend_params'))
            except:
                self.storeConnect = {}
        # Zabbix接続で自己証明書の利用
        self.selfCert = True if CONFIG.get('self_cert') == 'YES' else False
        # ストア設定、デフォルト設定はZabbixCloneDataStore側にあるのでここにはない
        # ロール
        self.role = CONFIG.get('role', ZC_DERAULT_ROLE)
        # Zabbixエンドポイント
        self.endpoint = CONFIG.get('endpoint', 'http://localhost')
        # ZabbixCloudフラグ
        # エンドポイントでZabbixCloudを判定する
        self.zabbixCloud = True if re.match('https://([a-z0-1-]*).zabbix.cloud(/){0,1}', self.endpoint) else False
        self.platformPassword = CONFIG.get('platform_password', None)
        # 認証情報
        self.token = CONFIG.get('token', None)
        self.auth = [
            CONFIG.get('user', ZABBIX_DEFAULT_AUTH[0]),
            CONFIG.get('password', None)
        ]
        if self.role == 'worker':
            # 適用指定バージョン
            self.targetVersion = CONFIG.get('version', None)
            # ワーカーがデフォルトパスワードであれば管理者のパスワードを設定ファイル内のものに変更する
            self.updatePassword = True if CONFIG.get('update_password', 'NO') == 'YES' else False
        else:
            # マスターでバージョン指定は不要
            self.targetVersion = None
            # マスターノードのパスワードは操作しない
            self.updatePassword =False
        # Bacis/Digit認証利用
        self.httpAuth = True if CONFIG.get('http_auth', 'NO') == 'YES' else False
        if self.zabbixCloud:
            # ZabbixCloud対応: HTTP AUTHは無効
            self.httpAuth = False
        if self.httpAuth:
            # Basic/Digit認証利用ではアップデートできない
            self.updatePassword = False
            # トークンが使えない
            self.token = None
        # ワーカーノードの強制初期化
        self.forceInitialize = True if CONFIG.get('force_initialize', 'NO') == 'YES' else False
        if self.role == 'master':
            self.forceInitialize = False
        # 監視対象のエンドポイントIP利用の強制
        self.forceUseip = True if CONFIG.get('force_useip', 'NO') == 'YES' else False
        # 監視対象のアップデート許可
        self.forceHostUpdate = True if CONFIG.get('force_host_update', 'NO') == 'YES' else False
        # ストアデータにない対象の削除を行わない
        self.noDelete = True if CONFIG.get('no_delete', 'NO') == 'YES' else False
        # checknow実行
        self.checknowExec = True if CONFIG.get('checknow_execute', 'NO') == 'YES' else False
        # checknowの対象インターバル
        self.checknowInterval = CONFIG.get('checknow_interval', ['1h'])
        # checknowを実行する際の設定適用待機時間
        self.checknowWait = CONFIG.get('checknow_wait', 30)
        # 並列実行可能数
        self.phpWorkerNum = int(CONFIG.get('php_work_num', PHP_WORKER_NUM))
        # DBダイレクト接続設定（Zabbix Server設定を使わない場合の設定）
        self.dbConnect = CONFIG.get('db_connect', {})
        if self.dbConnect:
            if self.dbConnect.pop('type', 'pgsql') == 'pgsql':
                self.dbConnect['port'] = 5432
            else:
                self.dbConnect['port'] = 3306
        # ここから下のものはコマンド引数と環境変数で設定されない
        # Secret global macro対応
        self.secretGlobalmacro = CONFIG.get('secret_globalmacro', [])
        # データ取り込みを有効にするユーザーとそのパスワード（ZabbixAPIでパスワードは取れないので）
        self.enableUser = CONFIG.get('enable_user', {})
        # 特権管理者の複製を許可する
        self.cloningSuperAdmin = True if CONFIG.get('cloning_super_admin', 'NO') == 'YES' else False
        # Proxy PSK情報（ZabbixAPIでpskは取れないので）
        self.proxyPsk = CONFIG.get('proxy_psk', {})
        # グローバル設定
        self.settings = CONFIG.get('settings', {})
        # アラート設定、setAlertMedia()で操作
        self.mediaSettings = CONFIG.get('media_settings', {})
        for media, params in self.mediaSettings.copy().items():
            if not params.get('user'):
                self.mediaSettings.pop(media)
        # マスターノード追加情報
        self.description = CONFIG.get('description', None)
        # MFAシークレット情報
        self.mfaClientSecret = CONFIG.get('mfa_client_secret', {})
        # テンプレートのスキップ
        self.templateSkip = True if CONFIG.get('template_skip', 'NO') == 'YES' else False
        if self.forceInitialize:
            self.templateSkip = False
        # テンプレートのエクスポート時の区切り数
        self.templateSeparate = CONFIG.get('template_separate', ZC_TEMPLATE_SEPARATE)

        return ZC_COMPLETE

    def changeDirectMaster(self):
        '''
        Directモードのマスターとしてコンフィグ変更する
        '''
        self.directMaster = True
        self.role = 'master'
        self.node = self.storeConnect.get('directNode')
        self.endpoint = self.storeConnect.get('directEndpoint')
        self.token = self.storeConnect.get('directToken')
        self.auth = []
        self.httpAuth = False
        self.updatePassword = False
        self.templateSkip = False
        return ZC_COMPLETE

    def showParameters(self):
        '''
        パラメータ情報の表示
        （仮）
        '''
        print('[Zabbix Cloning Configurations]')
            
        # 設定ファイル関連
        if self.configFile:
            print(f'{TAB}Config File: {self.configFile}')
        else:
            print(f'{TAB}No Config Files Mode: YES')

        # ノード関連
        print(f'{TAB}Target Node: {self.node}')
        print(f'{TAB*2}Role: {self.role}')
        print(f'{TAB*2}Zabbix Endpoint: {self.endpoint}')
        if self.zabbixCloud:
            print(f'{TAB*2}ZabbixCloud Node: YES')

        # 認証関連
        if self.token:
            print(f'{TAB}Authentication Method: TOKEN')
        else:
            print(f'{TAB}Authentication Method: PASSWORD')
            print(f'{TAB*2}User: {self.auth[0]}')
        if self.updatePassword:
            print(f'{TAB}Update Password: YES')
        if self.httpAuth:
            print(f'{TAB}HTTP Basic/Digit Authentication Mode: YES')
        if self.selfCert:
            print(f'{TAB}Self Certification Use: YES')

        # 動作設定関連
        if self.forceInitialize:
            print(f'{TAB}Force Initialize with Worker: YES')
        if self.forceHostUpdate:
            print(f'{TAB}Force Update Exist Hosts: YES')
        if self.forceUseip:
            print(f'{TAB}Force Use IP Address Monitoring: YES')
        if self.noDelete:
            print(f'{TAB}Don\'t Delete Worker-Node Items: YES')
        if self.checknowExec:
            print(f'{TAB}Execute CheckNow after Host Cloning: Yes')
            print(f'{TAB*2}CheckNow TargetInterval: {self.checknowInterval}')
            print(f'{TAB*2}CheckNow Wait Sec for Data Apply: {self.checknowWait}')
        if self.templateSkip:
            print(f'{TAB}Configuration Import/Export Skip Template: Yes')
        if self.templateSeparate != ZC_TEMPLATE_SEPARATE and self.role == 'master':
            print(f'{TAB}Configuration Export Separate Count: {self.templateSeparate}')
        if self.phpWorkerNum != PHP_WORKER_NUM:
            print(f'{TAB}Number of Parallel Excution Create/Update Hosts: {self.phpWorkerNum}') 

        # ストア関連
        if self.storeType == 'dydb':
            storeType = 'AWS DynamoDB'
        elif self.storeType == 'redis':
            storeType = 'Redis'
        elif self.storeType == 'direct':
            storeType = 'Master-Node Zabbix Direct'
        elif self.storeType == 'file':
            storeType = 'Local File'
        else:
            storeType = f'Extend Store {self.storeType}'
        print(f'{TAB}Store Type: {storeType}')
        if self.storeType == 'dydb':
            if self.storeConnect.get('aws_region'):
                region = self.storeConnect['aws_region']
                print(f'{TAB*2}AWS Region: {region}')
        elif self.storeType == 'redis':
            ep = self.storeConnect['redis_host'] + ':' + str(self.storeConnect['redis_port'])
            print(f'{TAB*2}Redis Endpoint: {ep}')
        elif self.storeType == 'direct':
            node = self.storeConnect['direct_node']
            ep = self.storeConnect['direct_endpoint']
            print(f'{TAB*2}Master-Node: {node} ({ep})')
        elif self.storeType == 'extend':
            for name, item in self.storeConnect:
                print(f'{TAB*2}Extend Store Parameter {name}: {item}')
        else:
            pass

        # DB関連
        if self.dbConnect:
            print(f'{TAB}Custom DB Connection: ')
            for param in ['host', 'name', 'port', 'user', 'password']:
                if self.dbConnect.get(param):
                    if param == 'password':
                        item = 'Custom Password'
                    else:
                        item = self.dbConnect[param]
                    print(f'{TAB*2}DB{param.capitalize()}: {item}')

        # 暗号化関連
        if self.secretGlobalmacro:
            macros = ', '.join([macro['macro'] for macro in self.secretGlobalmacro])
            print(f'{TAB}Set Secret GlobalMacro: {macros}')
        if self.proxyPsk:
            proxies = ', '.join(self.proxyPsk.keys())
            print(f'{TAB}Set Proxy PSK: {proxies}')

        # グローバル設定関連
        if self.settings:
            print(f'{TAB}Set Custom Global Settings:')
            origin = {"1":'Information', "2":'Warning', "3":'Average', "4":'High', "5":'Disaster'}
            for lv, param in self.settings.get('severity', {}).items():
                print(f'{TAB*2}AlertLevel.{lv}:')
                name = param.get('name')
                color = param.get('color')
                if name:
                    print(f'{TAB*3}ChangeName: {origin[lv]} -> {name}')
                if color:
                    print(f'{TAB*3}ChangeColor: {color}')
            for timeout, second in self.settings.get('timeout', {}).items():
                print(f'{TAB*2}Timeout {timeout}: {second}')
        if self.enableUser:
            users = ', '.join(self.enableUser.keys())
            print(f'{TAB}Enable Cloning User: {users}')
        if self.mediaSettings:
            medias = ', '.join(self.mediaSettings.keys())
            print(f'{TAB}Use Custom MediaType Setting: {medias}')
            for media, params in self.mediaSettings.items():
                users = ', '.join([user[0] for user in params['user']])
                if users:
                    print(f'{TAB}MediaType[{media}] Set User(s): {users}')
        if self.mfaClientSecret:
            mfa = ', '.join(self.mfaClientSecret.keys())
            print(f'{TAB}MFA Client Secret (MFA Setting Requierd): {mfa}')
        return

class ZabbixCloneParameter():
    '''
    ZabbixAPIのパラメータのバージョン間差異を吸収するクラス
    '''

    def __init__(self, version):
        version = version if version else {'major': ZC_DEFAULT_ZABBIX_VERSION, 'minor': 0}

        # ベース:4.0
        methodParameters = {
            'hostgroup': {
                'id': 'groupid',
                'name': 'name',
                'options': {
                    'output': 'extend',
                },
            },
            'host': {
                'id': 'hostid',
                'name': 'host',
                'options': {
                    'output': ['hostid', 'host'],
                    'selectTags': ['tag', 'value']
                },
            },
            'template': {
                'id': 'templateid',
                'name': 'name',
                'options': {
                    'output': ['templateid', 'name'],
                },
            },
            'user': {
                'id': 'userid',
                'name': 'alias',
                'options': {
                    'output': ['alias', 'type'],
                    'getAccess': True,
                    'selectUsrgrps': ['name'],
                    'selectMedias': 'extend'
                },
            },
            'usergroup': {
                'id': 'usrgrpid',
                'name': 'name',
                'options': {
                    'output': 'extend',
                    'selectTagFilters': 'extend',
                    'selectRights': 'extend'
                }
            },
            'usermacro': {
                # ユーザーマクロはConfigurationでhostsの中に入ってくるのでここではグローバルマクロのみを対象とする
                'id': 'globalmacroid',
                'name': 'macro',
                'options': {
                    'output': ['macro', 'value'],
                    'globalmacro': True
                }
            },
            'mediatype': {
                'id': 'mediatypeid',
                'name': 'description', 
                'options': {
                    'output': 'extend',
                },
            },
            'action': {
                'id': 'actionid',
                'name': 'name',
                'options': {
                    'output': 'extend',
                    'selectOperations': 'extend',
                    'selectRecoveryOperations': 'extend',
                    'selectAcknowledgeOperations': 'extend',
                    'selectFilter': 'extend',
                    'search': {'conditiontype': [2]}, #トリガー直接指定のフィルターを除外
                },
            },
            'maintenance': {
                'id': 'maintenanceid',
                'name': 'name',
                'options': {
                    'selectGroups': 'extend',
                    'selectHosts': 'extend',
                    'selectTimeperiods': 'extend',
                    'selectTags': 'extend'
                },
            },
            'script': {
                'id': 'scriptid',
                'name': 'name',
                'options': {
                }
            },
            'valuemap': {
                'id': 'valuemapid',
                'name': 'name',
                'options': {
                    'output': 'extend',
                    'selectMappings': 'extend'
                }
            },
            'proxy': {
                'id': 'proxyid',
                'name': 'host',
                'options': {
                    'output': [ # PSK鍵をストアに保存しないようにするためAPIで取得しない
                        'host',
                        'status',
                        'proxy_address',
                        'tls_connect',
                        'tls_accept',
                        'tls_issuer',
                        'tls_subject',
                        'description'
                    ],
                    'selectInterface': ['useip', 'ip', 'dns', 'port']
                },
            },
            'drule': { # ネットワークディスカバリ
                'id': 'druleid',
                'name': 'name',
                'options': {
                    'output': 'extend',
                    'selectDChecks': 'extend'
                }
            },
            'correlation': {
                'id': 'correlationid',
                'name': 'name',
                'options': {
                    'output': 'extend',
                    'selectOperations': 'extend',
                    'selectFilter': 'extend',
                    'filter': {'status': ZABBIX_ENABLE}
                }
            },
            # Triggerはテンプレートに紐づいているものだけしかとらないのでAPIで取得しないようここには設定しない
        }

        # ベース:4.0
        sections = {
            # 一般設定グループ
            'GLOBAL': [],
            # Configuration.export操作グループ（{Method名: ファイル内Section名}）
            'CONFIG_EXPORT': {
                'hostgroup': 'groups',
                'template': 'templates',
                'host': 'hosts',
                'valuemap': 'valueMaps',
                'trigger': 'triggers',
            },
            # Configration.import操作グループ（名前が変わっても同じメソッドに変換する）
            'CONFIG_IMPORT': {},
            # Configurationの前に実行するグループ（Method名）
            'PRE': [
                'usermacro',
                'mediatype',
                'proxy',
            ],
            # Configurationの中間（templateとhostの間）に実行するグループ（Method名）
            'MID': [
                'script',
            ],
            # Configuration後に実行するグループ（Method名）
            'POST': [
                'action',
                'maintenance',
                'drule',
                'correlation',
            ],
            # アカウント関連の処理をするグループ（Method名）
            'ACCOUNT': [
                'usergroup', 
                'user',
            ],
            # 最後に実行される特別処理のグループ（Method名）
            'EXTEND': [],
            # DBダイレクト操作グループ（テーブル名）
            'DB_DIRECT': [
                'regexps',
                'expressions',
                'config',
            ],
        }

        # ベース:4.0
        importRules = {
            'applications': {
                'createMissing': True,
                'deleteMissing': True,
            },
            'groups': {
                'createMissing': True
            },
            'hosts': {
                'createMissing': True,
                'updateExisting': True,
            },
            'templateLinkage': {
                'createMissing': True,
                'deleteMissing': True,
            },
            'templates': {
                'createMissing': True,
                'updateExisting': True,
            },
            'items': {
                'createMissing': True,
                'updateExisting': True,
                'deleteMissing': True,
            },
            'discoveryRules': {
                'createMissing': True,
                'updateExisting': True,
                'deleteMissing': True,
            },
            'triggers': {
                'createMissing': True,
                'updateExisting': True,
                'deleteMissing': True,
            },
            'valueMaps': {
                'createMissing': True,
                'updateExisting': True,
            },
            # 以下、現在未対応なのでインポート操作しない
            'images': {
                'createMissing': False,
                'updateExisting': False,
            },
            'maps': {
                'createMissing': False,
                'updateExisting': False,
            },
            'screens': {
                'createMissing': False,
                'updateExisting': False,
            },
            'graphs': {
                'createMissing': False,
                'updateExisting': False,
                'deleteMissing': False,
            },
            'templateScreens': {
                'createMissing': False,
                'updateExisting': False,
                'deleteMissing': False,
            },
            'httptests': {
                'createMissing': False,
                'updateExisting': False,
                'deleteMissing': False,
            },
        }

        # 破棄するパラメーター
        discardParameter = {
            'host': ['items', 'triggers', 'discovery_rules'],
            'action': ['actionid', 'operationid', 'opcommand_hstid', 'opcommand_grpid'],
            'proxy': ['interface', 'lastaccess', 'version', 'compatibility', 'state', 'auto_compress'],
            'drule': ['nextcheck'],
            'authentication': {
                'ldap': [
                    'ldap_host',
                    'ldap_port',
                    'ldap_base_dn',
                    'ldap_search_attribute',
                    'ldap_bind_dn',
                    'ldap_case_sensitive',
                    'ldap_bind_password',
                    'ldap_userdirectoryid',
                    'ldap_jit_status',
                    'jit_provision_interval',
                ],
                'saml': [
                    'saml_idp_entityid',
                    'saml_sso_url',
                    'saml_slo_url',
                    'saml_username_attribute',
                    'saml_sp_entityid',
                    'saml_nameid_format',
                    'saml_sign_messages',
                    'saml_sign_assertions',
                    'saml_sign_authn_requests',
                    'saml_sign_logout_requests',
                    'saml_sign_logout_responses',
                    'saml_encrypt_nameid',
                    'saml_encrypt_assertions',
                    'saml_case_sensitive',
                    'saml_jit_status',
                ]
            }
        }

        # CONFIG_IMPORTの生成
        sections['CONFIG_IMPORT'][4.0] = {}
        for method, section in sections['CONFIG_EXPORT'].items():
            if method == 'valuemap':
                section = 'value_maps'
            sections['CONFIG_IMPORT'][4.0].update({section: method})

        # メジャーバージョンアップで追加されたメソッド、下位バージョンはこれみてスキップする
        addMethods = {}

        # DB直接操作、削除されたカラム
        dbConfigDropCols = {}

        # DB直接操作、configテーブルのカラム名変更
        dbConfigRenameCols = {}

        # 7.0新機能 個別のタイムアウト設定
        timeoutTarget = []

        # 7.0以降対応
        # ZabbixCloudで対応が必要な要素
        zabbixCloudSpecialItem = {
            'mediatype': [
                'Cloud Email'
            ],
            'role': [
                'modules',
                'modules.default_access'
            ],
            'authentication': [
                'http_auth_enabled',
                'http_login_form',
                'http_strip_domains',
                'http_case_sensitive'
            ]
        }

        # 4.4対応
        addMethods[4.4] = ['autoregistration']
        if version['major'] >= 4.4:
            # グローバル設定の自動登録設定のAPI化
            methodParameters.update(
                {
                    'autoregistration': {
                        'id': None,
                        'name': None,
                        'options': {}
                    }
                }
            )
            sections['GLOBAL'].append('autoregistration')
            # METHOD:mediatypeのキー名description->name
            # MediaTypeのAPI -> CONFIG_EXPORT移動
            methodParameters['mediatype']['name'] = 'name'
            methodParameters['mediatype']['options']['output'] = ['name']
            sections['PRE'].remove('mediatype')
            sections['CONFIG_EXPORT'].update({'mediatype': 'mediaTypes'})
            sections['CONFIG_IMPORT'][4.4] = {}
            sections['CONFIG_IMPORT'][4.4].update({'mediaTypes': 'mediatype'})
            importRules.update(
                {
                    'mediaTypes': {
                        'createMissing': True,
                        'updateExisting': True
                    }
                }
            )

        # 5.0対応
        if version['major'] >= 5.0:
            # usermacroにtype追加、textにのみ対応、secretはzc.conf読み込みで対応
            methodParameters['usermacro']['options']['filter'] = {'type': 0}
            # 不要になったカラム
            dbConfigDropCols.update(
                {
                    5.0: [
                        'dropdown_first_entry',
                        'dropdown_first_remember'
                   ]
                }
            )

        # 5.2対応
        # 追加Method
        addMethods[5.2] = ['role']
        if version['major'] >= 5.2:
            # usermacroにtype追加、vaultにも対応
            methodParameters['usermacro']['options']['filter'] = {'type': [0, 2]}
            # 権限管理がroleで詳細設定の追加、対象管理は引き続きusergroup
            methodParameters.update(
                {
                    'role': {
                        'id': 'roleid',
                        'name': 'name',
                        'options': {
                            'output': 'extend',
                            'selectRules': 'extend'
                        }
                    }
                }
            )
            # userの出力にroleidを追加
            methodParameters['user']['options']['output'].append('roleid')
            sections['POST'].append('role')
            # インポートルールtemplateScreens->templateDashboards
            importRules['templateDashboards'] = importRules.pop('templateScreens', {})
            # 不要になったカラム
            dbConfigDropCols.update(
                {
                    5.2: [
                        'refresh_unsupported'
                    ]
                }
            )
            discardParameter['role'] = ['readonly']

        # 5.4対応
        # 追加Method
        addMethods[5.4] = []
        if version['major'] >= 5.4:
            # METHOD:userのキー名変更、alias->username
            methodParameters['user']['name'] = 'username'
            methodParameters['user']['options']['output'] = ['username', 'roleid']
            # valuemapのホスト／テンプレート内への埋め込みによる項目削除（インポートルールは継続）
            sections['CONFIG_EXPORT'].pop('valuemap', None)
            # application/screens廃止に伴うインポートルールの削除
            importRules.pop('applications', None)
            importRules.pop('screens', None)
            # 不要になったカラム
            dbConfigDropCols.update(
                {
                    5.4: [
                        'compression_availability'
                    ]
                }
            )

        # 6.0対応
        # 追加Method
        addMethods[6.0] = ['authentication', 'regexp', 'settings', 'sla', 'service']
        if version['major'] >= 6.0:
            # 認証設定authenticationの追加、5.2で追加されたAPIだけど、設定のテーブルは同じconfigなので6.0で適用
            # グローバル設定のAPI化対応regexp/settings
            # SLA/Service追加、6.0で作り直されているのでそれ以降をサポート
            methodParameters.update(
                {
                    'authentication': {
                        'id': None,
                        'name': None,
                        'options': {}
                    },
                    'regexp': {
                        'id': 'regexpid',
                        'name': 'name',
                        'options': {
                            'output': ['regexpid', 'name'],
                            'selectExpressions': [
                                'expression', 
                                'expression_type', 
                                'exp_delimiter', 
                                'case_sensitive'
                            ]
                        }
                    },
                    'settings': {
                        'id': None,
                        'name': None,
                        'options': {}
                    },
                    'sla': {
                        'id': 'slaid',
                        'name': 'name',
                        'options': {
                            'output': 'extend',
                            'selectSchedule': 'extend',
                            'selectExcludedDowntimes': 'extend',
                            'selectServiceTags': 'extend',
                        }
                    },
                    'service': {
                        'id': 'serviceid',
                        'name': 'name',
                        'options': {
                            'output': 'extend',
                            'selectParents': ['name'],
                            'selectChildren': ['name'],
                            'selectStatusRules': 'extend',
                            'selectProblemTags': 'extend',
                            'selectTags': 'extend',
                        }
                    }
                }
            )
            # パラメータ名変更対応
            value = methodParameters['action']['options'].pop('selectAcknowledgeOperations', None)
            methodParameters['action']['options']['selectUpdateOperations'] = value
            # setGlobalsettingsで実行するグループ
            sections['GLOBAL'].extend(['settings', 'authentication'])
            sections['PRE'].append('regexp')
            sections['POST'].extend(['service', 'sla'])
            # グローバル設定と正規表現のAPI対応に伴うDBのダイレクト操作の廃止
            sections.pop('DB_DIRECT', None)
            discardParameter.update(
                {
                    'service': ['status', 'uuid', 'created_at', 'readonly'],
                    'settings': ['ha_failover_delay'],
                    'sla': ['service_tags', 'schedule', 'excluded_downtimes'],
                }
            )

        # 6.2対応
        # 追加Method
        addMethods[6.2] = ['templategroup']
        if version['major'] >= 6.2:
            # グループがホストとテンプレートでメソッド分離
            # テンプレートグループ追加
            methodParameters.update(
                {
                    'templategroup': {
                        'id': 'groupid',
                        'name': 'name',
                        'options': {
                            'output': 'extend'
                        }
                    }
                }                
            )
            # Maitenanceのホストグループ指定ワードの変更
            value = methodParameters['maintenance']['options'].pop('selectGroups', None)
            methodParameters['maintenance']['options']['selectHostGroups'] = value
            # Usergroupの権限指定ワードの変更
            value = methodParameters['usergroup']['options'].pop('selectRights', None)
            methodParameters['usergroup']['options'].update(
                {
                    'selectHostGroupRights': value,
                    'selectTemplateGroupRights': value,
                }
            )
            # オプションの変更groups -> host_groups、templategroup追加
            sections['CONFIG_EXPORT'].update(
                {
                    'hostgroup': 'host_groups',
                    'templategroup':'template_groups'
                }
            )
            sections['CONFIG_IMPORT'][6.2] = {}
            sections['CONFIG_IMPORT'][6.2].update(
                {
                    'host_groups': 'hostgroup',
                    'template_groups':'templategroup'
                }
            )
            # インポートルール変更
            # 6.0まで5.0からのインポートに必要なので6.2から不使用にする
            sections['CONFIG_IMPORT'][4.0].pop('value_maps', None)
            value = importRules.pop('groups', None)
            importRules.update(
                {
                    'host_groups': value,
                    'template_groups': value
                }
            )
            discardParameter['authentication']['ldap'].append('ldap_userdirectoryid')

        # 6.4対応
        # 追加Method
        addMethods[6.4] = ['userdirectory']
        if version['major'] >= 6.4:
            # LDAP/SAML対応
            methodParameters.update(
                {
                    'userdirectory': {
                        'id': 'userdirectoryid',
                        'name': 'name',
                        'options': {
                            'output': 'extend',
                            'selectProvisionMedia': 'extend',
                            'selectProvisionGroups': 'extend'
                        }
                    }
                }
            )
            # userでroleidとuserdirectoryidのどちらかが必要になったので追加
            methodParameters['user']['options']['output'].append('userdirectoryid')
            sections['POST'].append('userdirectory')
            # DBダイレクト操作は6.0で無しになったけど、一応名前変更カラムの情報定義
            dbConfigRenameCols.update(
                {
                    6.4: [
                        ('ldap_configured', 'ldap_auth_enabled')
                    ]
                }
            )
            discardParameter['authentication']['ldap'].extend(
                [
                    'ldap_jit_status',
                    'jit_provision_interval',
                ]
            )
            discardParameter['authentication']['saml'].append('saml_jit_status')
            discardParameter['role'].append('services.actions')
            
        # 7.0対応
        # 追加Method
        addMethods[7.0] = ['proxygroup', 'mfa']
        if version['major'] >= 7.0:
            # プロキシグループの追加
            # プロキシの設定大幅変更のため入れ替え
            # 認証にMFA追加
            methodParameters.update(
                {
                    'proxygroup': {
                        'id': 'proxy_groupid',
                        'name': 'name',
                        'options': {
                            'output': [
                                'proxy_groupid',
                                'name',
                                'failover_delay',
                                'min_online',
                                'description'
                            ]
                        }
                    },
                    'proxy': {
                        'id': 'proxyid',
                        'name': 'name',
                        'options': {
                            'output': 'extend'
                        }
                    },
                    'mfa': {
                        'id': 'mfaid',
                        'name': 'name',
                        'options': {
                            'output': 'extend'
                        }
                    }
                }
            )
            # proxyより先にproxygroupを処理する
            sections['PRE'].remove('proxy')
            sections['PRE'].append('proxygroup')
            sections['MID'].append('proxy')
            # MFAの方をauthenticationより先に処理する
            sections['POST'].append('mfa')
            # DBダイレクト操作は6.0で無しになったけど、一応廃止カラムの情報定義
            # 認証周りの設定がグローバル設定から削除 -> userdirectory
            dbConfigDropCols.update(
                {
                    7.0: [
                        'ldap_host',
                        'ldap_port',
                        'ldap_base_dn',
                        'ldap_bind_dn',
                        'ldap_bind_password',
                        'ldap_search_attribute',
                        'saml_idp_entityid',
                        'saml_sso_url',
                        'saml_slo_url',
                        'saml_username_attribute',
                        'saml_sp_entityid',
                        'saml_nameid_format',
                        'saml_sign_messages',
                        'saml_sign_assertions',
                        'saml_sign_authn_requests',
                        'saml_sign_logout_requests',
                        'saml_sign_logout_responses',
                        'saml_encrypt_nameid',
                        'saml_encrypt_assertions',
                        'dbversion_status',
                    ]
                }
            )
            # 個別のタイムアウト設定
            timeoutTarget = [
                'simple_check',
                'snmp_agent',
                'external_check',
                'db_monitor',
                'http_agent',
                'ssh_agent',
                'telnet_agent',
                'script',
                'browser'
            ]


        # クラス変数化
        # メソッドget実行のためのパラメータ
        self.methodParameters = methodParameters
        # Configurationでの変換処理実行するなどの区分
        self.sections = sections
        # Configuration.importルール
        self.importRules = importRules
        # メジャーバージョンアップで追加されたメソッド、下位バージョンはこれみてスキップする
        self.addMethods = addMethods
        # DB直接操作、削除されたカラム
        self.dbConfigDropCols = dbConfigDropCols
        # DB直接操作、configテーブルのカラム名変更
        self.dbConfigRenameCols = dbConfigRenameCols
        # メソッド内で除去するパラメーター
        self.discardParameter = discardParameter
        # 7.0対応 アイテム取得のタイムアウト分離
        self.timeoutTarget = timeoutTarget
        # 7.0以降 ZabbixCloudで対応が必要な要素
        self.zabbixCloudSpecialItem = zabbixCloudSpecialItem

        # ID Name->Method変換テーブル生成
        self.idMethod = {}
        for method, parameter in self.methodParameters.items():
            self.idMethod.update(
                {
                    parameter['id']: method
                }
            )
            # テンプレートグループとホストグループでID名が被ってる
            # テンプレートグループを変換で使うことはないのでホストグループ指定に強制
            self.idMethod.update({'groupid': 'hostgroup'})



    def getKeynameInMethod(self, method=None, key='id'):
        '''
        methodParametersからメソッドのID/NAMEキー名を取得する
        '''
        if method not in self.methodParameters.keys():
            return ''
        key = key if key in ['id', 'name'] else 'id'
        return self.methodParameters[method][key]

    def getMethodFromIdname(self, idName=None):
        '''
        ID Nameからメソッド名を返す
        '''
        return self.idMethod.get(idName, None)

class ZabbixCloneDatastore():
    '''
    データストアクラス
    データストア側が持ちやすい形のフォーマットへの変換と読み書き

    データ構造：
        'VERSION': {
            'VERSION_ID': 'uuid4で生成',
            'UNIXTIME': 'unixtimeのタイムスタンプ、DynamoDBだとDecimalになってるので注意',
            'MASTER_VERSION': 'バージョン生成時のマスターノードのZabbixバージョン',
            'DESCRIPTION': '生成したマスターノードの情報',
            'EXPIRE': 'dydbで削除実行時の時間+1hのUNIXTIME'
        }
        'DATA': {
            'VERSION_ID': 'このレコードが属するバージョン',
            'METHOD': '≒Zabbix API Method（それ以外のもあるから））',
            'NAME': 'ZABBIX内での名前',
            'DATA': 'データ本体',
            'EXPIRE': 'dydbで削除実行時の時間+1hのUNIXTIME'
        }
    DynamoDB: テーブル名の接頭語として'ZC_'をつける
        VERSION: そのまま、パーティションキーはVERSION_ID,ソートキーはTIMESTAMP
        DATA: パーティションキーはVERSION_ID、DATA_IDをuuid4でソートキー
        両方ともEXPIREで削除を有効
    redis   : VERSIONがdb0、DATAがdb1、データが全部binaryなのでencode/decodeに注意
        VERSION: VERSION_IDがkeyのhash、UNIXTIME/MASTER_VERSIONはそのままhash内のキー
        DATA: VERSION_IDがkeyのhash、{DATA_IDがハッシュ内キー: bz2圧縮JSONテキスト))}
    '''

    # ストア上のZabbixデータ（指定したバージョン）{'METHOD': [{},...]} 検索しないで処理するのでこの形
    STORE = {}
    # ストアから取得したバージョンデータ（全部）
    VERSIONS = {}
    # 使用するデータストアの種類
    storeType = ''
    # データストアの設定＆接続オブジェクト
    storeTables = {
        'VERSION': {
            'primary': 'VERSION_ID',
            'sort':'UNIXTIME',
            'client': None
        },
        'DATA': {
            'primary': 'VERSION_ID',
            'sort': 'DATA_ID',
            'client': None
        }
    }
    # 追加ストア指定
    extendStore = None
    # DynamoDBの負荷調整パラメータ
    dydbLimit = 10
    dydbWait = 2

    # エラーメッセージ関連
    MSG_NON_SUPPORT      = '%s: Non Supprt Datastore, %s.'
    MSG_CONNECTION_ERROR = '%s: Connection Error, Table:%s.'
    MSG_NO_CONFIG        = '%s: No Exist Connection Config.'
    MSG_FAILED_CLEAR     = '%s: Failed Clear, table:%s.\n%s'
    MSG_NO_EXIST_VERSION_CLIENT = 'No Exist VERSION client'
    
    def __init__(self, CONFIG):

        if not isinstance(CONFIG, ZabbixCloneConfig):
            sys.exit('ZabbixCloneDatastore, Bad Config.')

        # directMasterではデータストアの設定の必要なし
        # データストアへの接続情報
        self.storeType = CONFIG.storeType
        self.storeConnect = CONFIG.storeConnect
        result = self.initStoreSetting()
        if not result[0]:
            sys.exit(result[1])

        # デフォルト対応以外のデータストア
        if self.storeType not in ['redis', 'dydb', 'file']:
            try:
                # インポートの試行
                import importlib
                module = 'extendDatastore' + self.storeType.capitalize()
                self.extendStore = importlib.import_module(module)
            except:
                sys.exit(f'Non Suppoer Datastore, {self.storeType}')

    # ファンクション共通化
    def functionWrapper(self, **params):
        '''
        各ファンクションの共通処理ラッパー
        呼び出し名 + クラス初期化で設定されたストアのファンクションを実行
        '''
        result = ZC_COMPLETE
        try:
            # このラッパーを呼び出したファンクションの名前
            funcName = inspect.stack()[1].function
        except:
            return (False, 'functionWrapper() cannot be executed directly.')
        # 実ファンクションの指定
        function = getattr(self, funcName + self.storeType.capitalize(), None)
        if not function:
            # デフォルトになければエクステンドストアから指定
            function = getattr(self.extendStore, funcName, None)
            params['storeConnect'] = self.storeConnect
        if function:
            # ファンクションの実行
            result = function(**params) if params else function()
        else:
            # ファンクションがない＝指定のストアに対応していない
            result = (False, self.MSG_NON_SUPPORT % (funcName, self.storeType))
        return result

    # ストア初期化関連
    def initStoreSetting(self):
        '''
        ストアの接続設定初期化
        '''
        result = self.functionWrapper(storeConnect=self.storeConnect)
        if result[0]:
            self.storeTables = result[1]
            result = ZC_COMPLETE
        return result

    def initStoreSettingDydb(self, storeConnect):
        '''
        DynamoDB設定初期化
        '''
        result = (True, self.storeTables)

        # 負荷調整パラメーター
        self.dydbLimit = storeConnect.get('dydbLimit', self.dydbLimit)
        self.dydbWait = storeConnect.get('dydbWait', self.dydbWait)

        # 接続インスタンス生成
        if storeConnect.get('awsAccessId') and storeConnect.get('awsSecretKey'):
            # 設定の認証情報で初期化
            dydb = boto3.resource(
                'dynamodb',
                aws_access_key_id=storeConnect['awsAccessId'],
                aws_secret_access_key=storeConnect['awsSecretKey'],
                region_name=storeConnect['awsRegion']
            )
        else:
            # 環境変数認証ファイル、IAM Roleでの初期化
            try:
                dydb = boto3.resource('dynamodb')
            except:
                result = (False, self.MSG_NO_CONFIG % self.storeType)

        # テーブル操作初期化
        if result[0]:
            for table in self.storeTables.keys():
                self.storeTables[table].update(
                    {
                        'client': dydb.Table(ZC_HEAD + table)
                    }
                )
                try:
                    # テーブルの有効確認
                    if self.storeTables[table]['client'].table_status != 'ACTIVE':
                        result = (False, '%s: No-Active Table, %s' % (self.storeType, table))
                except:
                    # 実行失敗
                    result = (False, self.MSG_CONNECTION_ERROR % (self.storeType, table))
        
        return result

    def initStoreSettingRedis(self, storeConnect):
        '''
        Redis設定初期化
        '''
        result = (True, self.storeTables)

        # 接続情報の確認
        if storeConnect.get('redisHost') and storeConnect.get('redisPort'):
            # 接続設定の初期化
            idx = 0 # redisのDB番号
            for table in self.storeTables.keys():
                # bz2圧縮するのでdecode_responsesは不使用
                connectInfo = {
                    'host': storeConnect['redisHost'],
                    'port': storeConnect['redisPort'],
                    'db': idx,
                    'max_connections': 4
                }
                if storeConnect.get('redisPassword'):
                    connectInfo['password'] = storeConnect['redisPassword']
                pool = redis.ConnectionPool(**connectInfo)
                self.storeTables[table]['client'] = redis.StrictRedis(connection_pool=pool)
                # 接続確認
                try:
                    self.storeTables[table]['client'].info()
                except:
                    result = (False, self.MSG_CONNECTION_ERROR % (self.storeType, table))
                idx += 1
        else:
            result = (False, self.MSG_NO_CONFIG % self.storeType)

        return result

    def initStoreSettingFile(self, storeConnect):
        '''
        ダミー
        '''
        return (True, self.storeTables)

    # 各ストア独自のファンクション
    def dydbNum(self, d=None):
        '''
        小数点はstr数字はDecimal
        '''
        if isinstance(d, str) and '.' in d:
            try:
                d = float(d)
            except:
                d = None
        else:
            try:
                d = int(d)
            except:
                d = None
        return d

    def dydbScan(self, table=None, projection=[]):
        '''
        1回1MBを超えた場合の対策Scan
        taeble: 対象のDynamoDBテーブル
        projection: 取得するAttribute
        '''
        if not table:
            return {'Items':[], 'Count': 0}
        # 取得対象指定
        client = self.storeTables[table]['client']
        params = {'ProjectionExpression': ','.join(projection)} if projection else {}
        try:
            res = client.scan(**params)
        except:
            return {'Items':[], 'Count': 0}
        Items = res['Items']
        # 継続キーが入ってたらなくなるまで繰り返し
        while 'LastEvaluatedKey' in res:
            params.update({'ExclusiveStartKey': res['LastEvaluatedKey']})
            res = client.scan(**params)
            Items.extend(res['Items'])
        return {'Items': Items, 'Count': len(Items)}

    def dydbQuery(self, table=None, version=''):
        '''
        DynamoDB Queryラッパー
        フィルターは１つだけ
        '''
        if table not in self.storeTables.keys() or not version:
            return {'Items':[], 'Count': 0}
        client = self.storeTables[table]['client']
        # キー指定
        params = {'KeyConditionExpression': Key(self.storeTables[table]['primary']).eq(version)}
        res = client.query(**params)
        Items = res['Items']
        # 継続キーが入ってたらなくなるまで繰り返し
        while 'LastEvaluatedKey' in res:
            params.update({'ExclusiveStartKey': res['LastEvaluatedKey']})
            res = client.query(**params)
            Items.extend(res['Items'])
        return {'Items': Items, 'Count': len(Items)}

    # ストア全消去
    def clearStore(self, table='ALL'):
        '''
        ストア上のデータすべて削除
        '''
        if table == 'ALL':
            tables = ['VERSION', 'DATA']
        elif table in ['VERSION', 'DATA']:
            tables = [table]
        else:
            return (False, 'required ALL / VERSION / DATA, tables:%s.' % table)
        return self.functionWrapper(tables=tables)

    def clearStoreDydb(self, tables):
        '''
        DynamoDBストアリセット
        '''
        result = ZC_COMPLETE

        for table in tables:
            client = self.storeTables[table]['client']
            primary_key = self.storeTables[table]['primary']
            sort_key = self.storeTables[table]['sort']
            data = self.dydbScan(table, [primary_key, sort_key])
            if not data['Count']:
                # データがなかったら飛ばす
                continue
            # バッチ処理
            count = 0
            try:
                with client.batch_writer() as batch:
                    for row in data['Items']:
                        item = {
                            'Key': {
                                primary_key: row[primary_key],
                                sort_key: row[sort_key]
                            }
                        }
                        try:
                            batch.delete_item(**item)
                        except:
                            break
                        # 負荷調整、dydbLimit*10件ごとに1秒の待機
                        count += 1
                        if count > self.dydbLimit*10:
                            sleep(self.dydbWait)
                            count = 0
            except Exception as e:
                result = (False, self.MSG_FAILED_CLEAR % (self.storeType, tables, e))

        return result

    def clearStoreRedis(self, tables):
        '''
        Redisストアリセット
        '''
        result = ZC_COMPLETE

        try:
            for table in tables:
                self.storeTables[table]['client'].flushall()
        except Exception as e:
            result = (False, self.MSG_FAILED_CLEAR % (self.storeType, tables, e))
        return result

    def deleteRecordInStore(self, versionId='', dataId=''):
        '''
        未実装
        対象バージョンの特定レコードをDATAテーブルから消す
        dydb: 実行時刻から1時間後のEXPIREを設定して、DynamoDB側に消させる
        redis: 即削除実行
        '''
        if not versionId:
            return (False, 'No Exist Version ID.')
        if not dataId:
            return (False, 'No Exist Data ID.')
        try:
            uuid.UUID(versionId)
            uuid.UUID(dataId)
        except:
            return (False, 'versionId/dataId Must be UUID.')
        return self.functionWrapper(version=versionId, data=dataId)

    def deleteRecordInStoreDydb(self, version, data):
        return (False, f'{version}/{data}')

    def deleteRecordInStoreRedis(self, version, data):
        return (False, f'{version}/{data}')

    def deleteVersionInStore(self, versionId=''):
        '''
        未実装
        対象バージョンをVERSION/DATAテーブルから消す
        dydb: 実行時刻から1時間後のEXPIREを設定して、DynamoDB側に消させる
        redis: 即削除実行
        '''
        if not versionId:
            return (False, 'No Exist Version.')
        try:
            uuid.UUID(versionId)
        except:
            return (False, 'versionId Must be UUID.')
        return self.functionWrapper(version=versionId)

    def deleteVersionInStoreDydb(self, version):
        return (False, f'{version}')

    def deleteVersionInStoreRedis(self, version):
        return (False, f'{version}')

    def getDatasetFromFile(self, versionId):
        '''
        未実装
        データストアにJSONファイルから移植する
        VERSION/DATAともに1ファイルに入っている
        defaultDir: /ver/lib/zabbix/zc/datastore/
        filename: {versionId}.json
        '''
        result = ZC_COMPLETE
        if not versionId:
            return (False, 'No Exist Version.')
        try:
            uuid.UUID(versionId)
        except:
            return (False, 'versionId Must be UUID.')
        return result
    
    def getVersionFromStore(self, version=''):
        '''
        version: ターゲットバージョン、Noneならすべて
        '''
        result = self.functionWrapper(
            version=version,
            client=self.storeTables['VERSION']['client']
        )
        if result[0]:
            # TIMESTAMPで降順に整列（[0]が最新）してクラス変数に入れる
            self.VERSIONS = sorted(result[1], key=lambda x:x['UNIXTIME'], reverse=True)
        else:
            result = (False, '%s: %s' % (self.storeType, result[1]))

        return result

    def getVersionFromStoreDydb(self, **params):
        '''
        DynamoDBからVERSIONの全データを取得
        返値: (boolean, versions)
        '''
        version = params.get('version')
        versions = []
        try:
            # 1MB以上のダウンロードに対応したスキャンファンクションを使う
            dls = self.dydbScan('VERSION')
            dls = [dl for dl in dls['Items'] if dl['VERSION_ID'] == version] if version else dls['Items']
            for dl in dls:
                # 成型して追加
                versions.append(
                    {
                        'VERSION_ID': dl['VERSION_ID'],
                        'UNIXTIME': self.dydbNum(dl['UNIXTIME']),
                        'MASTER_VERSION': self.dydbNum(dl['MASTER_VERSION']),
                        'DESCRIPTION': dl['DESCRIPTION']
                    }
                )
            result = (True, versions)
        except Exception as e:
            result = (False, str(e))
        return result

    def getVersionFromStoreRedis(self, **params):
        '''
        RedisからVERSIONの全データを取得
        返値: (boolean, versions)
        '''
        version = params.get('version')
        client = params.get('client')
        if not client:
            return (False, self.MSG_NO_EXIST_VERSION_CLIENT)
        versions = []
        try:
            # Redisスキャン
            dls = client.scan()
            dls = [dl.decode() for dl in dls[1]]
            if version in dls:
                # ターゲットバージョンのみ取得
                dls = [version]
            for id in dls:
                # バリューの取得
                dl = client.hgetall(id)
                # 成型して追加
                versions.append(
                    {
                        'VERSION_ID': id,
                        'UNIXTIME': int(dl[b'UNIXTIME']),
                        'MASTER_VERSION': float(dl[b'MASTER_VERSION']),
                        'DESCRIPTION': dl[b'DESCRIPTION'].decode()
                    }
                )
            result = (True, versions)
        except Exception as e:
            result = (False, str(e))
        return result

    def getVersionFromStoreFile(self, **params):
        '''
        ディレクトリのファイルリストを取得
        '''
        version = params.get('version')
        versions = []
        # Windowsとその他でディレクトリを変える
        if os.environ.get('os') == 'Windows_NT':
            # c:\user\アカウント\マイドキュメント\zc\{uuid}.json
            path = os.path.join(
                os.environ.get('userprofile'),
                ZC_FILE_STORE[1],
                'zc'
            )
        else:
            # /var/lib/zabbix/zc/{uuid}.json
            path = os.path.join(
                ZC_FILE_STORE[0],
                'zc'
            )
        # ファイル名の取得
        files = [item for item in os.listdir(path) if os.path.isfile(os.path.join(path, item))]
        # タイムスタンプの取得
        for file in files:
            desc = file
            file = file.removesuffix('.bz2').split('_')
            if version:
                if version != file[0]:
                    continue
            versions.append(
                {
                    'VERSION_ID': file[0],
                    'UNIXTIME': int(file[1]),
                    'MASTER_VERSION': float(file[2]),
                    'DESCRIPTION': f'Import File {desc}'
                }
            )
        return (True, versions)

    def setVersionToStore(
            self,
            VERSION_ID='__NOT_YET_CLONE__',
            UNIXTIME=UNIXTIME(),
            MASTER_VERSION=str(ZC_DEFAULT_ZABBIX_VERSION),
            DESCRIPTION=''
        ):
        '''
        ストアにバージョンデータを追加する
        '''
        version = {
            'VERSION_ID':VERSION_ID,
            'UNIXTIME': UNIXTIME,
            'MASTER_VERSION': str(MASTER_VERSION),
            'DESCRIPTION': str(DESCRIPTION)
        }
        client = self.storeTables['VERSION']['client']
        result = self.functionWrapper(version=version, client=client)
        if not result[0]:
            result = (False, '%s: %s\n%s' % (self.storeType, result[1], json.dumps(version)))
        return result

    def setVersionToStoreDydb(self, **params):
        '''
        DynamoDBにバージョンデータを追加する
        返値: (boolean, message)
        '''
        result = ZC_COMPLETE
        version = params.get('version')
        if not version:
            return (False, 'No Exist VERSION data')
        client = params.get('client')
        if not client:
            return (False, self.MSG_NO_EXIST_VERSION_CLIENT)
        try:
            # 実行
            res = client.put_item(**{'Item': version})
            resCode = res['ResponseMetadata'].get('HTTPStatusCode')
            if resCode != 200:
                result = (False, 'Bad Response put_item, %s.' % resCode)
        except Exception as e:
            # エラー
            result = (False, 'Except put_item, %s.' % e)
        return result

    def setVersionToStoreRedis(self, **params):
        '''
        Redisにバージョンデータを追加する
        返値: (boolean, message)
        '''
        result = ZC_COMPLETE
        version = params.get('version')
        if not version:
            return (False, 'No Exist VERSION data')
        client = params.get('client')
        if not client:
            return (False, self.MSG_NO_EXIST_VERSION_CLIENT)
        # キーは別パラメーターなので取り出す
        versionId = version.pop('VERSION_ID', None)
        try:
            # 実行
            res = client.hset(versionId, mapping=version)
            if not res:
                result = (False, 'Bad Response VERSION hset.')
        except Exception as e:
            # エラー
            result = (False, 'Except VERSION hset, %s.' % e)
        return result

    def setVersionToStoreFile(self, **params):
        '''
        ダミー
        '''
        return ZC_COMPLETE

    def getDataFromStore(self, version=None):
        '''
        ストアから対象のバージョンのDATAを取得する
        返値: [{method: [],...}]
        '''
        if not version:
            return (False, [])
        client = self.storeTables['DATA']['client']
        if not client and not self.storeType == 'file':
            return (False, [])
        result = self.functionWrapper(version=version, client=client)
        return result

    def getDataFromStoreDydb(self, **params):
        '''
        DynamoDBから対象バージョンのDATAを取得する
        返値: (boolean, [{item},...])
        '''
        data = []
        version = params['version']
        # VERSION_IDでフィルタしてダウンロード
        items = self.dydbQuery('DATA', version['VERSION_ID'])
        if not items['Count']:
            return (False, data)
        for item in items['Items']:
            try:
                # {METHOD:'', 'DATA_ID': '', 'NAME':'', 'DATA': b'encodedValue'})',...}
                # DATAのvalueを取り出してbz2でコード、json.loadsでdictに変換
                item['DATA'] = json.loads(bz2.decompress(item['DATA'].value).decode())
                data.append(item)
            except Exception as e:
                print(e)
                return (False, data)
        return (True, data)

    def getDataFromStoreRedis(self, **params):
        '''
        Redisから対象バージョンのDATAを取得する
        返値: (boolean, [{item},...])
        '''
        version = params['version']
        client = params['client']
        data=[]
        try:
            version = version['VERSION_ID']
            # Redisスキャン
            scan = client.scan()
            if version not in [item.decode() for item in scan[1]]:
                return (False, 'No Exist %s.' % version)
            items = client.hgetall(version)
            # 成型して追加
            for dataId, item in items.items():
                # データのbz2解凍
                item = json.loads(bz2.decompress(item).decode())
                data.append(
                    {
                        'DATA_ID': dataId.decode(),
                        'METHOD': item['METHOD'],
                        'NAME': item['NAME'],
                        'DATA': item['DATA']
                    }
                )
        except Exception as e:
            print(e)
            return (False, f'{e}')

        return (True, data)

    def getDataFromStoreFile(self, **params):
        '''
        ストアデータをファイルから読み込む
        '''
        version = params['version']

        file = '%s_%s_%s.%s' % (
            version['VERSION_ID'],
            version['UNIXTIME'],
            version['MASTER_VERSION'],
            'bz2'
        )

        # Windowsとその他でディレクトリを変える
        if os.environ.get('os') == 'Windows_NT':
            # c:\user\アカウント\マイドキュメント\zc\{uuid}_{timestamp}_{ZabbixVer}.bz2
            file = os.path.join(
                os.environ.get('userprofile'),
                ZC_FILE_STORE[1],
                'zc',
                file
            )
        else:
            # /var/lib/zabbix/zc/{uuid}_{timestamp}_{ZabbixVer}.bz2
            file = os.path.join(
                ZC_FILE_STORE[0],
                'zc',
                file
            )

        # ファイル読み込み
        if os.path.exists(file) and os.access(file, os.R_OK):
            try:
                with open(file, 'rb') as f:
                    self.STORE = json.loads(bz2.decompress(f.read()).decode())
            except Exception as e:
                return (False, 'Cannot Read %s, %s' % (file, e))

        return ZC_COMPLETE

    def setDataToStore(self, version=None):
        '''
        ストアにデータを追加する
        '''
        result = ZC_COMPLETE
        if not version or not self.STORE:
            return (False, 'Bad Parameters.')
        client = self.storeTables['DATA']['client']
        if not client and not self.storeType == 'file':
            return (False, 'No Exist DATA Client.')
        # DATA_IDを追加
        for items in self.STORE.values():
            for item in items:
                item.update({'DATA_ID': str(uuid.uuid4())})
        # 実行
        result = self.functionWrapper(version=version, dataset=self.STORE, client=client)
        if not result[0]:
            return (False, '%s: %s' % (self.storeType, result[1]))
        return result

    def setDataToStoreDydb(self, **params):
        '''
        DynamoDBにデータを追加する
        返値: (boolean, message)
        '''
        result = ZC_COMPLETE
        version = params['version']
        dataset = params['dataset']
        client = params['client']
        # データをDynamoDBのテーブルに合わせて１レコード１アイテムに変換
        # 1レコード400KBの制限があるのでDATAはbz2圧縮、大きいのはテンプレートのデータ
        setItems = []
        for method, items in dataset.items():
            for item in items:
                setItems.append(
                    {
                        'VERSION_ID': version['VERSION_ID'],
                        'DATA_ID': item['DATA_ID'],
                        'METHOD': method,
                        'NAME': item['NAME'],
                        'DATA': bz2.compress(json.dumps(item['DATA'], ensure_ascii=False).encode())
                    }
                )
        # DynamoDBバッチ処理
        count = 0
        with client.batch_writer() as batch:
            for item in setItems:
                try:
                    batch.put_item(**{'Item': item})
                except Exception as e:
                    result = (False, 'Faild batch execute put_item, %s' % e)
                    break
                # 負荷調整処理、dydbLimit数ごとにdydbWait秒待機する
                # DynamoDBのWrite側インスタンス数設定に注意すること、AutoScalingしてると負荷によってはめっちゃでかくなる
                count += 1
                if count > self.dydbLimit:
                    sleep(self.dydbWait)
                    count = 0
        return result

    def setDataToStoreRedis(self, **params):
        '''
        Redisにデータを追加する
        返値: (boolean, message)
        '''
        result = ZC_COMPLETE
        # dataset = {'METHOD': [{'DATA_ID': '', 'NAME': '', 'DATA': {ZabbixMethodData}}], {},...}
        version = params['version']
        dataset = params['dataset']
        client = params['client']
        # データ変換、dict->JSON->bz2圧縮
        data = {}
        try:
            for method, items in dataset.items():
                for item in items:
                    data.update(
                        {
                            item['DATA_ID']: bz2.compress(
                                json.dumps(
                                    {
                                        'METHOD': method,
                                        'NAME': item['NAME'],
                                        'DATA': item['DATA']
                                    },
                                    ensure_ascii=False
                                ).encode()
                            )
                        }
                    )
            res = client.hset(version['VERSION_ID'], mapping=data)
            if not res:
                result = (False, 'Bad Response DATA hset')
        except Exception as e:
            result = (False, 'Except DATA hset, %s' % e)
        return result

    def setDataToStoreFile(self, **params):
        '''
        ストアデータをファイルに書き込む
        '''
        version = params.get('version')
        result = ZC_COMPLETE
        if not version:
            return (False, 'version Empty.')
        
        file = '%s_%s_%s.%s' % (
            version['VERSION_ID'],
            version['UNIXTIME'],
            version['MASTER_VERSION'],
            'bz2'
        )

        # Windowsとその他でディレクトリを変える
        if os.environ.get('os') == 'Windows_NT':
            # c:\user\アカウント\マイドキュメント\zc\{uuid}_{X.Y}.json
            path = os.path.join(
                os.environ.get('userprofile'),
                ZC_FILE_STORE[1],
                'zc'
            )
        else:
            # /var/lib/zabbix/zc/{uuid}.json
            path = os.path.join(
                ZC_FILE_STORE[0],
                'zc'
            )

        # ファイル読み込み
        if os.path.exists(path) and os.access(path, os.W_OK):
            file = os.path.join(path, file)
            try:
                with open(file, mode='wb') as f:
                    f.write(bz2.compress(json.dumps(self.STORE, ensure_ascii=False).encode()))
            except Exception as e:
                result = (False, 'Cannot Write %s, %s' % (file, e))
        else:
            result = (False, 'No Such or Not Writable %s' % path)
        return result

class ZabbixClone(ZabbixCloneParameter, ZabbixCloneDatastore):
    '''
    Zabbixのデータ複製操作クラス
    '''

    def __init__(self, CONFIG):

        # pyzabbixインスタンス
        self.ZAPI = None
        # 生成した新バージョンデータ
        self.NEW = {}
        # ノード上のZabbixデータ{'METHOD': {'NAME': {}},{'NAME': {}}...} 名前で検索するのでこの形
        self.LOCAL = {}
        # Zabbix IDとZabbix Nameの変換テーブル
        self.IDREPLACE = {}
        # ノードのZabbixバージョン
        self.VERSION = None

        # 設定の適用
        if not isinstance(CONFIG, ZabbixCloneConfig):
            sys.exit('ZabbixClone, Bad Config.')
        if not CONFIG.result[0]:
            sys.exit(CONFIG.result[1])
        self.CONFIG = CONFIG
        # APIクライアントの初期化
        result = self.initZabbixApi()
        if not result[0]:
            sys.exit(result[1])
        self.ZAPI = result[1]
        # 実行対象のZabbix Version取得
        try:
            self.VERSION = self.ZAPI.api_version().split('.')
            self.VERSION = {
                'major': float('.'.join(self.VERSION[:2])),
                'minor': int(self.VERSION[-1])
            }
        except:
            print('Failed Get zabbix version info.')
            sys.exit(2)

        # 権限確認
        if not self.CONFIG.token:
            data = {}
            try:
                # 5.4対応 キー名の変更
                if self.VERSION['major'] >= 5.4:
                    name = 'username'
                else:
                    name = 'alias'
                data = self.ZAPI.user.get(output='extend', filter={name: self.CONFIG.auth[0]})
                data = data[0]
            except:
                print('Failed, get %s Information.' % self.CONFIG.auth[0])
                sys.exit(2)
            if self.VERSION['major'] >= 5.2:
                permit = 'roleid'
            else:
                permit = 'type'
            if int(data.get(permit)) != ZABBIX_SUPER_ROLE:
                print('No SuperAdministrator Permission, %s.' % data[name])
                sys.exit(2)

        # Zabbix DB接続設定、6.0以降はDB直接接続は使用しない
        if self.VERSION['major'] < 6.0:
            result = self.initDbConnect()
            if not result[0]:
                print(result[1])
                sys.exit(3)

        # 継承クラスの初期化（ZabbixCloneParameter）
        ZabbixCloneParameter.__init__(self, self.VERSION)

        if self.CONFIG.storeType != 'direct':
            # データストアを初期化、パラメーターはデータストア内のを使う
            # self.VERSIONS/self.STOREはZabbixCloneDatastore()のクラス変数
            ZabbixCloneDatastore.__init__(self, self.CONFIG)

    def checkMasterNode(self):
        '''
        ノードに設置されている設定ファイルから、今実行しているノードがマスターノードか確認
        '''
        return True if self.CONFIG.role == 'master' else False

    def initZabbixApi(self):
        '''
        pyZabbixのクライアントイニシャライズ
        '''

        # ZabbixAPIインスタンス
        API = ZabbixAPI(self.CONFIG.endpoint)

        # 接続先の名称確認
        # APIで取れるようになったらそっちを使う
        result = CHECK_ZABBIX_SERVER_NAME(self.CONFIG.endpoint, self.CONFIG.node)
        if not result[0]:
            return result

        # トークン
        token = self.CONFIG.token
        # パスワード
        auth = self.CONFIG.auth

        # 認証情報がない
        if not token and not auth[1]:
            return (False, 'No Exist Credentials.')

        # 自己証明書を使う
        if self.CONFIG.selfCert:
            API.session.verify = False

        # HTTP認証対応、コンフィグ生成時にパスワード変更とトークンは無効にされている
        if self.CONFIG.httpAuth:
            API.session.auth = auth
            auth = auth[:1]

        # トークンで認証確認
        if token:
            try:
                API.login(api_token=token)
                if not self.CONFIG.updatePassword:
                    # トークンで認証したのでパスワード認証しない
                    auth = None
                else:
                    # パスワード変更するのでパスワード認証もする
                    token = ''
            except Exception as e:
                # 認証できなかったトークンは消す
                token = None

        # パスワードで認証確認
        if not token and auth:
            try:
                API.login(*auth)
                # 変更後のパスワードで認証できたので更新しない
                self.CONFIG.updatePassword = False
                if token == '':
                    # 一度はトークン認証通しているのでそちらで認証しなおし（トークン優先）
                    token = self.CONFIG.token
                    API.login(token)
            except Exception as e:
                if self.CONFIG.updatePassword:
                    pass
                else:
                    # 最終的に認証に失敗
                    return (False, 'Incorrect Credentials.')

        # パスワード更新の場合はトークン認証してない場合デフォルト認証を試行する
        if self.CONFIG.updatePassword and not token:
            if self.CONFIG.platformPassword:
                # ZabbixCloud対応: プラットフォームがAdminのデフォルトパスワード生成
                auth = [ZABBIX_DEFAULT_AUTH[0], self.CONFIG.platformPassword]
            else:
                auth = ZABBIX_DEFAULT_AUTH
            try:
                API.login(*auth)
            except:
                return (False, 'Cannot Autneticate for ChangePassword.')

        return (True, API)

    def initDbConnect(self):
        '''
        DB接続設定のイニシャライズ
        '''

        # DB設定のデフォルト、そろってなかった場合もデフォルト使用
        dbConnect = {
            'DBName': 'file',
            'DBHost': 'file',
            'DBPort': '-1',
            'DBPassword': 'password',
            'DBUser': 'user',
        }

        # ZabbixServer設定読み込み
        serverConf = os.path.join(ZABBIX_CONFIG_PATH, ZABBIX_SERVER_CONFIG)
        if os.path.exists(serverConf) and os.access(serverConf, os.R_OK):
            # Zabbix Server設定のデータベース設定を取得
            with open(serverConf, 'r') as f:
                serverConf = [
                    conf.strip().split('=') for conf in f.readlines() if conf.strip() != '' and conf[0] != '#'
                ]
            [
                dbConnect.update(
                    {conf[0]: conf[1]}
                ) for conf in serverConf if len(conf) == 2 and conf[0] in dbConnect.keys()
            ]
            # 成型{'dbConnect': {'name':'', 'host':'', 'port':'', 'user':'', 'password':'', 'library':''}}
            for conf, value in dbConnect.items():
                # db_xxxxxxx で全部小文字
                dbConnect.update(
                    {
                        conf[3:].lower() : value
                    }
                )

        # クラスに渡されたパラメーターからの適用
        if self.CONFIG.dbConnect:
            for conf, value in dbConnect.items():
                dbConnect.update(
                    {
                        conf: self.CONFIG.dbConnect.get(conf, value)
                    }
                )

        if not self.CONFIG.dbConnect:
            return (False, 'DB Connector: No Exist Configurations.')

        # ポート番号からライブラリの指定
        if self.CONFIG.dbConnect['port'] == 5432:
            self.CONFIG.dbConnect['library'] = 'psycopg2'
        elif self.CONFIG.dbConnect['port'] == 3306:
            self.CONFIG.dbConnect['library'] = 'pymysql'
        else:
            self.CONFIG.dbConnect['library'] = 'sqlite3'
        # モジュール読み込み
        try:
            import importlib
            self.dbConnector = importlib.import_module(self.CONFIG.dbConnect.get('library'))
        except Exception as e:
            return (False, 'DB Connector: Initialize, Failed. %s' % e)

        return ZC_COMPLETE

    def changePassword(self, *auth):
        '''
        パスワード変更
        auth: [user, changePasswd, currentPasswd]
        パスワード変更は失敗しても処理を止めさせないのでTrueを返す（ここに来るのに認証は通っている）
        '''
        if not self.CONFIG.updatePassword:
            return (True, 'Already Update.')
        result = ZC_COMPLETE
        idName = self.getKeynameInMethod('user', 'id')
        name = self.getKeynameInMethod('user', 'name')
        auth = auth if len(auth) > 2 else self.CONFIG.auth
        currentPasswd = auth[2] if len(auth) == 3 else ZABBIX_DEFAULT_AUTH[1]
        # ZabbixCloud対応: プラットフォーム生成のデフォルトパスワードを指定する
        if self.CONFIG.platformPassword:
            currentPasswd = self.CONFIG.platformPassword

        try:
            # 対象管理者の確認
            admin = self.ZAPI.user.get(output=[idName, name], filter={name: auth[0]})
            if not admin:
                result = (True, 'No Exist User, %s.' % auth[0])
            else:
                # パスワード変更
                change = {
                    idName: admin[0][idName],
                    'passwd': auth[1]
                }
                # 6.4対応 現在のパスワードが必要
                if self.VERSION['major'] >= 6.4:
                    change.update({'current_passwd': currentPasswd})
                self.ZAPI.user.update(**change)
                # 変更したパスワードで再認証
                self.ZAPI.login(*auth)
        except Exception as e:
            result = (False, 'Failed Update Password for %s. %s' % (auth[0], e))

        return result

    def firstProcess(self):
        '''
        インスタンスの初期化後に最初に行うマスター/ワーカー共通処理
        ・バージョン情報の取得
        ・Zabbixからデータ初回取得
        ・データストアから最新バージョンの取得
        ・必須ホストグループの確認、追加、名前変更
        ・プロキシの削除
        '''
        result = ZC_COMPLETE

        # バージョン情報の取得
        if self.CONFIG.storeType == 'direct':
            # DirectMaster用バージョンの生成
            self.VERSIONS = [
                {
                    'VERSION_ID': '__DIRECT_MASTER_%s__' % ZABBIX_TIME(),
                    'TIMESTAMP': -1,
                    'DESCRIPTION': ''
                }
            ]
        else:
            result = self.getVersionFromStore()
            if result[0]:
                if not self.VERSIONS:
                    if self.checkMasterNode():
                        # これから作るので仮バージョンを生成
                        self.VERSIONS = [
                            {
                                'VERSION_ID': '__FIRST_CREATE__',
                                'TIMESTAMP': -1,
                                'MASTER_VERSION': self.VERSION['major'],
                                'DESCRIPTION': ''
                            }
                        ]
                    else:
                        # ワーカー側はストアのバージョンデータがないので実行不可
                        result = (False, 'No Exist On-Store Versions.')
                else:
                    pass
            else:
                # 取得失敗
                result = (False, 'Failed Get Versions.')

        if not result[0]:
            return result

        # ワーカーのZabbixバージョンがマスターのZabbixバージョンより古い場合は終了
        if not self.checkMasterNode and self.VERSION['major'] < self.getLatestVersion('MASTER_VERSION'):
            return (False, '%s zabbix version > Onstore Data zabbix version.' % self.CONFIG.node)

        # データの初回取得
        result = self.getDataFromZabbix()
        if not result[0]:
            return result

        # アラート通知ユーザー確認
        # デフォルト通知ユーザーがいるか確認
        alertUser = self.LOCAL['user'].get(ZC_NOTICE_USER)
        if not alertUser:
            return (False, 'Failed, firstProcess Need Alert User.')
        else:
            data = alertUser['DATA']
            # ユーザーが有効か確認
            if data.get('users_status', ZABBIX_DISABLE) != ZABBIX_ENABLE:
                return (False, 'Failed, firstProcess Notified User enabled')
            else:
                # デフォルト通知ユーザーが特権管理者か確認
                # 5.2対応 権限管理変更 type -> role
                if self.VERSION['major'] >= 5.2:
                    permit = 'roleid'
                else:
                    permit = 'type'
                if int(data.get(permit, -1)) != ZABBIX_SUPER_ROLE:
                    return (False, 'Failed, firstProcess Notified User Permission is not SuperAdministorator.')

        if self.checkMasterNode():
            # マスターノードの処理
            # ホストに管理UUIDタグをつける
            # ワーカーでアップデートするときにホストのユニーク情報として使う予定（ホスト名変更があるとZCではわからないので）
            
            # 表示（仮）
            print(f'\n{TAB*2}Set Host UUID:', end='', flush=True)
            count = 0

            for item in self.LOCAL['host'].values():
                # ユニーク識別のタグが付いていないことを確認
                if ZC_UNIQUE_TAG not in [tag['tag'] for tag in item['DATA']['tags']]:
                    # UUIDを生成して追加
                    item['DATA']['tags'].append(
                        {
                            'tag': ZC_UNIQUE_TAG,
                            'value': str(uuid.uuid4())
                        }
                    )
                    idName = self.getKeynameInMethod('host', 'id')
                    option = {
                        idName: item['ZABBIX_ID'],
                        'tags': item['DATA']['tags']
                    }
                    try:
                        # 適用実行
                        self.ZAPI.host.update(**option)
                        result = ZC_COMPLETE
                    except Exception as e:
                        result = (False, 'Failed, firstProcess set host uuid-tag. %s' % e)
                        
                    # 表示（仮）
                    if count == WIDE_COUNT or count == 0:
                        print(f'\n{TAB*3}', end='', flush=True)
                        count = T_COUNT*3
                    res = 'S' if result[0] else 'X'
                    print(f'{res}', end='', flush=True)
                    count += 1
                
            # 表示（仮）
            if count:
                print('')
            else:
                print(' Already set All Hosts.')

        else:
            # ワーカーノード側処理
            # 適用バージョンの確認
            version = self.CONFIG.targetVersion
            if version is None:
                version = self.getLatestVersion('VERSION_ID')
            elif version not in [item['VERSION_ID'] for item in self.VERSIONS]:
                version = self.getLatestVersion('VERSION_ID')
                lostVersion = self.CONFIG.targetVersion
                self.CONFIG.targetVersion = False
            else:
                # 適用バージョンを先頭に入れ替え（getLatest～の値変更）
                version = [item for item in self.VERSIONS if item['VERSION_ID'] == version]
                self.VERSIONS.remove(version[0])
                self.VERSIONS.insert(0, version[0])
                version = version[0]['VERSION_ID']
            
            # 表示（仮）
            info = self.getLatestVersion('DESCRIPTION')
            if info:
                info = info.replace(', ', f'\n{TAB*3}')
            print(f'\n{TAB*2}Cloning Version: {version}', flush=True)
            if self.CONFIG.targetVersion is False:
                print(f'{TAB*3}Change Latest, No Exist:{lostVersion}.', flush=True)
            if info:
                print(f'{TAB*2}Version Information:\n{TAB*3}{info}', flush=True)

            # 適用状態の確認
            nowVersion = self.LOCAL['usermacro'].get(ZC_VERSION_CODE, None) if not self.CONFIG.forceInitialize else None
            if nowVersion:
                nowVersion = nowVersion['DATA']['value']
                try:
                    # バージョン文字列がUUIDか確認
                    uuid.UUID(nowVersion)
                except:
                    if re.match('__DIRECT_MASTER_[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z__', nowVersion):
                        # マスター直接適用なのでパス
                        pass
                    else:
                        # バージョン文字列が不正なので初期化対象
                        nowVersion = None
                        # 初期化対象はテンプレートインポートスキップキャンセル
                        self.CONFIG.templateSkip = False
            if nowVersion:
                # ワーカーノードの設定削除否定フラグ
                if not self.CONFIG.noDelete:

                    # 表示（仮）
                    print(f'{TAB*2}Method Data Reset:', flush=True)

                    # 適用後も毎回リセットする対象
                    for method in ['correlation', 'drule', 'action', 'script', 'maintenance']:
                        api = getattr(self.ZAPI, method)
                        function = 'delete'
                        ids = [item['ZABBIX_ID'] for item in self.LOCAL[method].values()]
                        if ids:
                            try:
                                getattr(api, function)(*ids)
                            except Exception as e:
                                # 実行失敗で処理中止
                                result = (False, 'Failed, firstProcess Delete %s, %s.' % (method, e))

                        # 表示（仮）
                        res = 'Success' if result[0] else 'Failed'
                        print(f'{TAB*3}{method}: {res}.')

                        if not result[1]:
                            break

            else:
                # バージョン情報がないので初期化する
                
                # 表示（仮）
                print(f'{TAB*2}Start Initialize:', flush=True)
                print(f'{TAB*3}Method Data Clear:', flush=True)

                # イニシャライズ対象のメソッド、グループは要素があると消せないので最後に消す
                methods = ['usermacro', 'correlation', 'drule', 'mediatype', 'action', 'script', 'maintenance', 'host', 'proxy', 'template', 'hostgroup']
                # 6.0対応
                if self.VERSION['major'] >= 6.0:
                    methods = ['service', 'sla', 'regexp'] + methods
                # 6.2対応
                if self.VERSION['major'] >= 6.2:
                    # hostgroupからtemplategroupに分離されたので追加
                    methods.append('templategroup')
                    # settingsのdiscovery_groupidが削除不能のデフォルトHG
                    systemGroup = self.LOCAL['settings']['discovery_groupid']['DATA']['discovery_groupid']
                else:
                    # 6.0以前はシステムフラグありホストグループが削除不可
                    systemGroup = [item['ZABBIX_ID'] for item in self.LOCAL['hostgroup'].values() if int(item['DATA'].get('internal', 0))]
                    systemGroup = systemGroup[0]
                # 7.0対応
                if self.VERSION['major'] >= 7.0:
                    methods.append('proxygroup')
                # ZABBIXデフォルト設定の削除
                for method in methods:
                    api = getattr(self.ZAPI, method)
                    function = 'delete'
                    if method == 'hostgroup':
                        # systemGroupは削除不能（実行するとエラー）なので外す
                        ids = [item['ZABBIX_ID'] for item in self.LOCAL[method].values() if item['ZABBIX_ID'] != int(systemGroup)]
                    else:
                        ids = []
                        for item in self.LOCAL[method].values():
                            if self.CONFIG.zabbixCloud and item['NAME'] in self.zabbixCloudSpecialItem.get(method, []):
                                # ZabbixCloud対応: mediatypeの'Cloud Mail'消せないので除外
                                continue
                            else:
                                ids.append(item['ZABBIX_ID'])
                    if method == 'usermacro':
                        function += 'global'
                    if ids != []:
                        try:
                            getattr(api, function)(*ids)
                        except Exception as e:
                            # 実行失敗で処理中止
                            result = (False, 'Failed, firstProcess Default Delete %s, %s.' % (method, e))
                    
                    # 表示（仮）
                    res = 'Success' if result[0] else 'Failed'
                    print(f'{TAB*4}{method}: {res}.')

                    if not result[1]:
                        break

                if result[1]:
                    # バージョン情報の挿入
                    self.setVersionCode(init=True)
                    # 表示（仮）
                    print(f'{TAB*2}Set VersionCode Globalmacro.')

        if result[1]:
            self.getDataFromZabbix()
            # 表示（仮）
            print(f'{TAB*2}Get Node Zabbix Data.', end='', flush=True)
        
        return result

    def createNewVersion(self):
        '''
        新しいバージョンデータを取得、存在しなければマスターノードでのみ生成
        '''
        description = f'MasterNode: {self.CONFIG.node} ({self.CONFIG.endpoint}), CreateDate: {ZABBIX_TIME()}'
        if self.CONFIG.description:
            description += f' : {self.CONFIG.description}'
        if not self.NEW and self.checkMasterNode():
            self.NEW = {
                'VERSION_ID': str(uuid.uuid4()),
                'UNIXTIME': UNIXTIME(),
                'MASTER_VERSION': self.VERSION['major'],
                'DESCRIPTION': description
            }
        return self.NEW

    def getLatestVersion(self, target=None):
        '''
        最新バージョンデータを返す
        target: 指定キーの内容を返す
        '''
        latest = self.VERSIONS[0] if len(self.VERSIONS) > 0 else {}
        return latest if not target else latest.get(target, latest)

    def updateDbData(self, table=None, data=None):
        '''
        DB操作: テーブル内の対象を更新
        '''
        return self.operateDbDirect('update', table, data)

    def replaceDbData(self, table=None, data=None):
        '''
        DB操作: テーブルの全データ入れ替え
        '''
        return self.operateDbDirect('replace', table, data)

    def getDbData(self, table=None):
        '''
        DB操作: テーブルの全データ取得
        '''
        return self.operateDbDirect('get', table)

    def operateDbDirect(self, operate=None, table=None, tableData=None):
        '''
        DB操作ファンクション実体
        '''

        # パラメータ確認
        if operate not in ['get', 'update', 'replace']:
            return (False, 'Operate not get or update.')
        if operate in ['replace', 'update']:
            if not tableData:
                return (False, 'No Exist Table Data: %s' % table)
            if not isinstance(tableData, list):
                return (False, 'Wrong Replace/Update Table Data Type, %s.' % type(tableData))
        if not self.CONFIG.dbConnect:
            return (False, 'No Exist DB Connection Config.')
        dbConnect = self.CONFIG.dbConnect

        # 操作実行
        with self.dbConnector.connect(
            host=dbConnect['host'],
            port=dbConnect['port'],
            database=dbConnect['name'],
            user=dbConnect['user'],
            password=dbConnect['password']
        ) as connection:
            with connection.cursor() as cursor:
                if operate == 'get':
                    # 取得 
                    try:
                        cursor.execute('select * from %s' % table)
                        tableData = [[c[0] for c in cursor.description]]
                        [tableData.append(l) for l in cursor.fetchall()]
                        result = (True, tableData)
                    except:
                        result = (False, 'DB Direct Select %s, Failed.' % table)
                else:
                    # 自動コミットの停止
                    if dbConnect['library'] == 'psycopg2':
                        connection.autocommit = False
                    elif dbConnect['library'] == 'pymysql':
                        connection.autocommit(False)
                    else:
                        pass
                    if operate == 'replace':
                        # 置き換え
                        try:
                            cursor.execute('DELETE FROM %s' % table)
                        except:
                            result = (False, 'DB Direct Delete All data on %s, Failed.' % table)
                        try:
                            # ヘッダー生成
                            head = ','.join(tableData[0])
                            # 1行ずつSQL生成＆実行
                            for row in tableData[1:]:
                                row = '\'' + '\',\''.join(map(str, row)) + '\''
                                sql = 'INSERT INTO %s (%s) VALUES (%s)' % (table, head, row)
                                cursor.execute(sql)
                            result = ZC_COMPLETE
                        except Exception as e:
                            result = (False, 'DB Direct Insert into %s, Failed. %s' % (table, e))
                    elif operate == 'update':
                        # 更新
                        if len(tableData) != 2:
                            result = ('False', 'Wrong Data for %s' % table)
                        elif len(tableData[0]) != len(tableData[1]):
                            result = ('False', 'Wrong Head/Data length for %s' % table)
                        else:
                            try:
                                # 更新対象
                                where = '%s = \'%s\'' % (tableData[0][0], tableData[1][0])
                                update = ''
                                # 更新対象の生成
                                for col in range(1, len(tableData[1])):
                                    update += '%s = \'%s\', ' % (tableData[0][col], tableData[1][col])
                                # SQL実行
                                sql = 'UPDATE %s SET %s WHERE %s' % (table, update.strip(', '), where)
                                cursor.execute(sql)
                                result = ZC_COMPLETE
                            except Exception as e:
                                result = (False, 'DB Direct Update %s, Failed. %s' % (table, e))
                    else:
                        result = (False, 'No Operate.')
                    # 問題なければコミット
                    if result[0]:
                        connection.commit()
                    else:
                        connection.rollback()
        return result

    def replaceIdName(self, method=None, target=None):
        '''
        method.targetの変換
        targetがidならname、nameならidを返す
        '''
        if not method or not target:
            # パラメータなし
            return None
        if self.IDREPLACE.get(method, None) is None:
            # メソッドが存在しない
            return None
        try:
            # 数字が文字列で入ってきた場合の処理
            target = int(target)
        except:
            pass
        if method == 'mediatype':
            # メディアタイプの特別処理
            if target == 0:
                return '__ALL_MEDIA__'
            elif target == '__ALL_MEDIA__':
                return 0
            else:
                pass
        elif method == 'host':
            # ホストの特別処理
            if target == 0:
                return '__CURRENT_HOST__'
            elif target == '__CURRENT_HOST__':
                return 0
            else:
                pass
        elif method == 'proxy':
            if target == 0:
                return '__SERVER_DIRECT__'
            elif target == '__SERVER_DIRECT__':
                return 0
            else:
                pass  
        elif method == 'proxygroup':
            # プロキシグループの特別処理
            if target == 0:
                return '__NO_GROUP__'
            elif target == '__NO_GROUP__':
                return 0
            else:
                pass
        elif method in ['usergroup', 'hostgroup', 'templategroup']:
            # グループ系の特別処理
            if target == 0:
                return '__ALL_GROUP__'
            elif target == '__ALL_GROUP__':
                return 0
            else:
                pass
        else:
            pass
        return self.IDREPLACE[method].get(target, None)

    def processingMethodData(self, section=''):
        '''
        self.STORE上のsections['POST']のID変換対象のメソッドのデータをIDからNAMEに変換する
        '''
        result = ZC_COMPLETE
        display = []

        if not self.sections.get(section):
            return (False, 'No section:%s in sections.' % section)
        methods = self.sections[section]
        for method in methods:
            function = 'processing' + method[0].upper() + method[1:]
            rWord = 'NoProcessing'
            if function in self.__dir__():
                result = getattr(self, function)()
                rWord = 'Success' if result[0] else 'Failed'

            # 表示（仮）
            display.append(f'{TAB*3}{method}: {rWord}.')

            if not result[0]:
                break

        return (True, display) if result[0] else result

    def processingRegexp(self):
        '''
        regexpの加工
        '''
        result = ZC_COMPLETE
        if not self.STORE.get('regexp'):
            return (True, 'No Data, regexp.')
        
        for item in self.STORE['regexp']:
            data = item['DATA']
            for expression in data['expressions']:
                if int(expression['expression_type']) != 1:
                    # これを使用する１以外ではエラーになるので削除
                    expression.pop('exp_delimiter', None)
        
        return result

    def processingAction(self):
        '''
        ActionのID加工
        マスターノードはローカルデータを加工、ワーカーノードはストアデータを加工
        actionid/operationid/op*idは削除、他idは名称に置換
        userid/groupid/usrgrpid はid2nameでnameに変換
        '''
        result = ZC_COMPLETE
        if not self.STORE.get('action'):
            return (True, 'No Data, action.')

        # createに不要なパラメータ―
        readOnly = self.discardParameter['action']
        discardOperate = ['esc_period', 'esc_step_from', 'esc_step_to']
        discardNotTriggerAction = ['pause_symptoms', 'pause_suppressed', 'notify_if_canceled']


        items = []
        try:
            for item in self.STORE['action'].copy():
                data = item['DATA']
                # 有効でないアクションは除外
                if data['status'] == ZABBIX_DISABLE:
                    continue

                # キー名ゆれ対応
                operateType = ['operations', 'recoveryOperations', 'acknowledgeOperations']
                for target in operateType.copy():
                    if target != 'operations':
                        targetData = data.pop(target, None)
                        # get/create間表記ゆれ対応（O -> _o）
                        rename = target.replace('O', '_o')
                        if not targetData:
                            targetData = data.pop(rename, None)
                        # 6.0対応
                        if self.VERSION['major'] >= 6.0:
                            rename = rename.replace('acknowledge', 'update')
                        if not targetData:
                            targetData = data.pop(target, None)
                        if targetData:
                            data[rename] = targetData
                        # 入れ替え
                        operateType.remove(target)  
                        operateType.append(rename)

                eventSource = int(data['eventsource'])
                # トリガーアクション以外で不要なものの削除
                if eventSource != 0:
                    [data.pop(param, None) for param in discardNotTriggerAction]
                # アップデートはトリガー/サービスアクションでのみ使用
                if eventSource in [1, 2, 3]:
                    data.pop('update_operations', None)
                    data.pop('updateOperations', None)
                    data.pop('acknowledge_operations', None)
                    data.pop('acknowledgeOperations', None)
                # ネットワークディスカバリと自動登録で不要なものの削除
                if eventSource in [1, 2]:
                    data.pop('recovery_operations', None)
                    data.pop('recoveryOperations', None)
                    data.pop('esc_period', None)

                # ZABBIXが動的に付けるのでeval_formulaを削除する
                data['filter'].pop('eval_formula', None)
                # 計算式を自動にしているならformulaを削除
                if int(data['filter'].get('evaltype', 0)) < 3:
                    data['filter'].pop('formula', None)
                    custom_formula = False
                else:
                    # カスタム計算式判定を利用
                    custom_formula = True
                # アクション条件のID変換処理
                for filter_item in data['filter']['conditions']:
                    # 6.0以降で入っているとエラーになる項目を削除
                    if self.VERSION['major'] >= 6.0:
                        if not custom_formula:
                            filter_item.pop('formulaid', None)
                        if not filter_item.get('value'):
                            filter_item.pop('value', None)
                        if not filter_item.get('value2'):
                            filter_item.pop('value2', None)
                    # ID変換対象メソッドを決定
                    condType = int(filter_item['conditiontype'])
                    if condType == 0:
                        method = 'hostgroup'
                    elif condType == 1:
                        method = 'host'
                    elif condType == 13:
                        method = 'template'
                    else:
                        # 対応していない要素
                        # filter_item['conditiontype'] == '2':
                        # Trigger直指定はNode間で同定が難しいので非対応
                        continue
                    # ID変換を実行
                    filter_item.update(
                        {
                            'value': self.replaceIdName(method, filter_item['value'])
                        }
                    )

                # 変換
                for target in operateType:
                    if not data.get(target):
                        data.pop(target, None)
                        continue
                    for operate in data[target]:
                        # 不要データの削除
                        # 空データの削除
                        [operate.pop(param, None) for param in operate.copy().keys() if not operate.get(param)]
                        # ZABBIXが自動的に付けるIDを削除
                        [operate.pop(param, None) for param in readOnly]
                        # トリガーアクション以外では不要なものの削除
                        if eventSource != 0:
                            operate.pop('evaltype', None)
                        # ネットワークディスカバリと自動登録で不要なものの削除
                        if eventSource in [1, 2]:
                            [operate.pop(param, None) for param in discardOperate]
                        # 更新と復帰の処理
                        if target != 'operations':
                            # 6.0以前でここにある条件式は削除
                            operate.pop('evaltype', None)
                            # 全メディア通知の場合、メッセージ設定されている場合はメディアIDを削除
                            if int(operate.get('operationtype')) == 11:
                                operate['opmessage'].pop('mediatypeid', None)
                        # オペレーション内容の処理
                        for op in operate.copy().keys():
                            opData = operate.get(op)
                            if not opData:
                                # アクション実行内容がないものは削除
                                operate.pop(op)
                                continue
                            if isinstance(opData, dict):
                                # 辞書型データの処理
                                # 実行内容がないものを削除
                                [opData.pop(param, None) for param in opData.copy().keys() if not opData.get(param)]
                                # 削除対象
                                [opData.pop(param, None) for param in readOnly]
                                # ID変換
                                for param in opData.copy().keys():
                                    method = self.getMethodFromIdname(param)
                                    if not method:
                                        continue
                                    trans = self.replaceIdName(method, opData[param])
                                    if trans is None:
                                        continue
                                    opData[param] = trans
                            elif isinstance(opData, list):
                                # リスト型データの処理
                                transData = []
                                for opd in opData:
                                    if not isinstance(opd, dict):
                                        # 要素は全部Dictのはず
                                        continue
                                    for param in opd.keys():
                                        # 削除対象
                                        if param in readOnly:
                                            continue
                                        # ID変換
                                        method = self.getMethodFromIdname(param)
                                        trans = self.replaceIdName(method, opd[param])
                                        if trans is None:
                                            continue
                                        transData.append({param: trans})
                                opData = transData
                            else:
                                # dictでもlistでもないのは処理しない
                                pass
                            if not opData:
                                # 空になったものは捨てる
                                operate.pop(op, None)
                            operate[op] = opData
                items.append(item)
        except Exception as e:
            result = (False, 'processingAction: %s' % e)
        self.STORE['action'] = items
        return result

    def processingMediatype(self):
        '''
        MediatypeのID変換
        4.4で不要になるけど4.0/4.2で使う
        '''
        result = ZC_COMPLETE
        if not self.STORE.get('mediatype'):
            return (True, 'No Data, meidatype.')
        items = []
        try:
            # 処理はあとで
            pass
        except Exception as e:
            result = (False, 'processingMediatype: %s' % e)
        self.STORE['mediatype'] = items
        return result

    def processingScript(self):
        '''
        ScriptのID変換
        マスターノードはローカルデータを加工、ワーカーノードはストアデータを加工
        '''
        result = ZC_COMPLETE
        if not self.STORE.get('script'):
            return (True, 'No Data, script.')

        items =[]
        try:
            for item in self.STORE['script'].copy():
                data = item['DATA']
                # 共通処理
                # ID変換
                for method in ['usergroup', 'hostgroup']:
                    idName = self.getKeynameInMethod(method, 'id')
                    if data.get(idName):
                        data[idName] = self.replaceIdName(method, data[idName])

                if self.checkMasterNode():
                    # マスターノード処理
                    pass
                else:
                    # ワーカーノード処理
                    scriptType = int(data['type'])
                    scope = int(data.get('scope', 0))
                    # 5.4対応
                    if self.VERSION['major'] >= 5.4:
                        # Webhook script用パラメーターの削除
                        if scriptType != 0:
                            # Scriptではない
                            data.pop('execute_on', None)
                        if scriptType != 2:
                            # SSHではない
                            data.pop('authtype', None)
                            data.pop('publickey', None)
                            data.pop('privatekey', None)
                            if scriptType != 3:
                                # Telnetでもない
                                data.pop('username', None)
                                data.pop('password', None)
                                data.pop('port', None)
                        else:
                            # SSH/Telnetである
                            if int(data['authtype']) == 0:
                                # パスワード認証である
                                data.pop('publickey', None)
                                data.pop('privatekey', None)
                            else:
                                # 鍵認証である
                                data.pop('password', None)
                        if scriptType != 5:
                            # Wehhooではない
                            data.pop('timeout', None)
                            data.pop('parameters', None)
                        if scope not in [2, 4]:
                            # スコープがmanual host action/manual event actionではない
                            data.pop('menu_path', None)
                            data.pop('usrgrpid', None)
                            data.pop('host_access', None)
                            data.pop('confirmation', None)
                    # 6.4対応
                    if self.VERSION['major'] >= 6.4:
                        # URL用パラメーターの削除
                        if scriptType != 6:
                            data.pop('url', None)
                            data.pop('new_window', None)
                    # 7.0 対応
                    if self.VERSION['major'] >= 7.0:
                        # スコープがmanual host action/manual event actionではない
                        # またはmanualinputが0である
                        if scope not in [2, 4] or int(data.get('manualinput', 0)) == 0:
                            data.pop('manualinput', None)
                            data.pop('manualinput_prompt', None)
                            data.pop('manualinput_validator', None)
                            data.pop('manualinput_validator_type', None)
                            data.pop('manualinput_default_value', None)
                        else:
                            if int(data.get('manualinput_validator_type', 0)) == 1:
                                data.pop('manualinput_default_value', None)
                items.append(item)
        except Exception as e:
            result = (False, 'processingScript: %s' % e)
        self.STORE['script'] = items
        return result

    def processingMaintenance(self):
        '''
        Maintenanceのデータ加工
        マスターノードはローカルデータを加工、ワーカーノードはストアデータを加工
        maintenanceメソッドはcreateとgetで対象リストのキー名が違うので、マスター側で加工する
        '''
        result = ZC_COMPLETE
        if not self.STORE.get('maintenance'):
            return (True, 'No Data, maitenance.')

        items = []
        try:
            for item in self.STORE['maintenance'].copy():
                data = item['DATA']
                # 一回限りのメンテの期限切れを削除
                for period in data['timeperiods'].copy():
                    if int(period['timeperiod_type']) == 0:
                        if int(period['start_date']) + int(period['period']) < UNIXTIME():
                            data['timeperiods'].remove(period)
                        # 一回限りのメンテナンスで不要なものの削除
                        period.pop('start_time', None)
                        period.pop('every', None)
                        period.pop('day', None)
                        period.pop('dayofweek', None)
                        period.pop('month', None)
                    elif int(period['timeperiod_type']) == 1:
                        # 毎日に不要なものの削除
                        period.pop('start_date', None)
                        period.pop('dayofweek', None)
                    elif int(period['timeperiod_type']) == 2:
                        # 毎週に不要なものの削除
                        period.pop('start_date', None)
                        period.pop('day', None)
                    elif int(period['timeperiod_type']) == 3:
                        # 毎月に不要なものの削除
                        period.pop('start_date', None)
                    else:
                        pass
                # メンテ期間が空またはメンテウィンドウの終了が現在より後（期限切れ）を削除
                if not data['timeperiods']:
                    continue
                if int(data['active_till']) < UNIXTIME():
                    continue
                if self.checkMasterNode():
                    # 6.2対応
                    if self.VERSION['major'] >= 6.2:
                        hosts = 'hosts'
                        groups = 'hostgroups'
                    else:
                        hosts = 'hosts'
                        groups = 'groups'
                    # マスターノード側処理: 対象リストをIDのみに変換
                    # ホストグループリスト
                    name = self.getKeynameInMethod('hostgroup', 'name')
                    data[groups] = [target[name] for target in data.get(groups, [])]
                    if not data[groups]:
                        data.pop(groups)
                    # ホストリスト
                    name = self.getKeynameInMethod('host', 'name')
                    data[hosts] = [target[name] for target in data.pop(hosts, [])]
                    if not data[hosts]:
                        data.pop(hosts)
                    if not data['tags']:
                        data.pop('tags')
                else:
                    if self.VERSION['major'] >= 6.2:
                        hosts = 'hosts'
                        groups = 'groups'
                    else:
                        hosts = 'hostids'
                        groups = 'groupids'
                    # データ側のバージョンでの変更
                    if self.getLatestVersion('MASTER_VERSION') >= 6.2:
                        storeIds = {
                            hosts: 'hosts',
                            groups: 'hostgroups'
                        }
                    else:
                        storeIds = {
                            hosts: 'hosts',
                            groups: 'groups'
                        }
                    # ワーカーノード側処理: 対象リストの中を{idName: id}に変換
                    for section in [groups, hosts]:
                        targets = data.pop(storeIds[section], [])
                        method = 'host' if section == hosts else 'hostgroup'
                        id = self.getKeynameInMethod(method, 'id')
                        if targets:
                            data[section] = [
                                {
                                    id: self.replaceIdName(method, target)
                                } for target in targets if self.replaceIdName(method, target)
                            ]
                if not data.get(groups) and not data.get(hosts):
                    # グループもホストも空の場合はスキップ
                    continue
                items.append(item)
        except Exception as e:
            result = (False, 'processingMaintenance: %s' % e)
        self.STORE['maintenance'] = items
        return result

    def processingProxy(self):
        '''
        proxyのデータ加工
        バージョン共通: psk利用時のid/pskの代入
        >=7.0: プロキシグループのID変換
        '''
        result = ZC_COMPLETE
        if not self.STORE.get('proxy'):
            return (True, 'No Data, proxy.')

        items = []
        deleteTarget = []
        try:
            for item in self.STORE['proxy'].copy():
                data = item['DATA']
                if self.checkMasterNode():
                    # 7.0対応
                    if self.VERSION['major'] >= 7.0:
                        # プロキシグループのID変換
                        id = self.getKeynameInMethod('proxygroup', 'id')
                        data[id] = self.replaceIdName('proxygroup', data[id])
                else: 
                    # ワーカーノード処理
                    # 不要データを削除
                    for param in self.discardParameter['proxy']:
                        data.pop(param, None)
                    # 7.0系timeout系は上書きなしまたは空だったら削除
                    for timeout in [param for param in data if re.match('timeout_', param)]:
                        if int(data.get('custom_timeouts', 0)) == 0 or not data.get(timeout):
                            data.pop(timeout, None)
                    mode = int(data.get('status', 5)) - 5
                    # 7.0対応
                    if self.VERSION['major'] >= 7.0:
                        # active/passiveのモード判定、7.0に合わせて0:active/1:passive
                        id = self.getKeynameInMethod('proxygroup', 'id')
                        if self.getLatestVersion('MASTER_VERSION') >= 7.0:
                            # プロキシグループのID変換
                            data[id] = self.replaceIdName('proxygroup', data[id])
                            mode = data['operating_mode']
                        else:
                            # 以前のバージョンからの変換
                            data[id] = 0
                            data['name'] = data.pop('host', None)
                            data['allowed_addresses'] = data.pop('proxy_address', None)
                            data['operating_mode'] = mode
                            data.pop('status', None)
                    desc = data.get('description', '')
                    # プロキシの指定記述はdescriptionの先頭に「ZC_WORKER:node;」
                    # ZC_WORKERが無いまたは複数の記述があるプロキシは削除
                    if len(re.findall(ZC_MONITOR_TAG + ':[0-9a-zA-Z-_.]*', desc)) != 1:
                        continue
                    # Discriptionに自分のノード名が載ってないプロキシは削除
                    if not re.match(ZC_MONITOR_TAG + ':%s;' % self.CONFIG.node, desc):
                        if item['NAME'] in self.LOCAL['proxy'].keys():
                            # 自分に割り当てられなくなったプロキシ
                            deleteTarget.append(self.LOCAL['proxy'][item['NAME']]['ZABBIX_ID'])
                            pass
                        continue
                    # PSK利用の判定
                    if mode == 1:
                        # passive
                        usePsk = True if int(data['tls_connect']) == 2 else False
                    else:
                        # active 1:None,2:PSK,4:SSLのビットマップ、2が含まれない1,4,5じゃないことを判定
                        usePsk = True if int(data['tls_accept']) not in [1, 4, 5] else False
                    # 5.4以降はAPIで取れないようになるので設定ファイルに記載で統一
                    if usePsk:
                        psk = self.CONFIG.proxyPsk.get(item['NAME'], [])
                        try:
                            # PSKが16進法か確認
                            int(psk[1], 16)
                            # 適切な長さか確認（128bit以上2048bit以下）
                            if len(psk[1]) < 64 or len(psk[1]) > 1024:
                                psk = []
                        except:
                            psk = []
                        if len(psk) != 2:
                            # PSK情報が不正の場合はPSK未使用設定に変更
                            # プロキシが不在になるとホスト作成の方に処理を入れないといけないので削除はしない
                            if mode:
                                # passive 暗号化なしに変更
                                data['tls_connect'] = 1
                            else:
                                # active PSKフラグの2を引く、2の場合は1にする
                                data['tls_accept'] = int(data['tls_accept']) - 2 if int(data['tls_accept']) > 2 else 1
                            # descriptionにPSK無効にしたことを追記
                            pskDisableMessage = '[%s PSK DISABLED]' % ZABBIX_TIME()
                            if data.get('description'):
                                data['description'] = pskDisableMessage + '\r\n\r\n' + data['description']
                            else:
                                data['description'] = pskDisableMessage
                        else:
                            # PSK情報を設定
                            data['tls_psk_identity'], data['tls_psk'] = psk
                items.append(item)
        except Exception as e:
            result = (False, 'processingProxy: %s' % e)
        self.STORE['proxy'] = items
        # 削除対象がある場合
        if deleteTarget:
            self.STORE['proxyExtend'] = [{'delete': deleteTarget}]
            self.sections['EXTEND'].append('proxyExtend')
        return result

    def processingProxygroup(self):
        '''
        proxygroupのデータ加工
        プロキシグループの削除のみ
        '''
        result = ZC_COMPLETE
        if not self.STORE.get('proxygroup'):
            return (True, 'No Data, proxygroup.')

        deleteTarget = []
        if self.checkMasterNode():
            # マスターノード処理
            pass
        else:
            # ワーカーノード処理
            # 自身が対象ではなくなったプロキシグループの削除
            names = [item['NAME'] for item in self.STORE.get('proxygroup', [])]
            for name, item in self.LOCAL.get('proxygroup', {}).items():
                if name not in names:
                    deleteTarget.append(item['ZABBIX_ID'])
        if deleteTarget:
            self.STORE['proxygroupExtend'] = [{'delete': deleteTarget}]
            self.sections['EXTEND'].append('proxygroupExtend')
        return result

    def processingDrule(self):
        '''
        ストアのネットワークディスカバリデータを加工
        '''
        result = ZC_COMPLETE
        if not self.STORE.get('drule'):
            return (True, 'No Data, drule(Network Discvoery).')

        # dType
        all = list(range(0,16))
        agent = [9, 10 ,11, 13]
        snmpV1_2 = [10, 11]
        snmpV3 = [13]
        icmp = [12]
        tcp = all
        tcp.remove(12)

        items = []
        try:
            for item in self.STORE['drule'].copy():
                data = item['DATA']
                # プロキシの変換
                # 7.0でプロキシグループに対応していないので、7.2以降変更の可能性あり
                idRename = None
                if self.VERSION['major'] >= 7.0:
                    # ワーカーノードで7.0未満のマスターノードのデータ
                    if self.getLatestVersion('MASTER_VERSION') < 7.0 and not self.checkMasterNode():
                        idName = 'proxy_hostid'
                        idRename = 'proxyid'
                    else:
                        idName = 'proxyid'
                else:
                    idName = 'proxy_hostid'
                id = self.replaceIdName('proxy', data[idName])
                if id is None:
                    # 対応するプロキシーがなければ除外する
                    continue
                if idRename:
                    data[idRename] = id
                    data.pop(idName, None)
                else:
                    data[idName] = id
                if self.checkMasterNode():
                    # マスターノード処理
                    pass
                else:
                    # ワーカーノード処理
                    # 不要データの削除
                    [data.pop(param, None) for param in self.discardParameter['drule']]
                    # 7.0対応
                    data.pop('error', None)
                    for check in data['dchecks']:
                        dType = int(check['type'])
                        # ID系は共通で削除
                        check.pop('dcheckid', None)
                        check.pop('druleid', None)
                        # デフォルト値のものは削除
                        # 4.2追加 host_source, name_source 
                        defaultZaro = ['port', 'host_source', 'name_source']
                        for param in defaultZaro:
                            if int(check.get(param, 0)) == 0:
                                check.pop(param, None)
                        # エージェントタイプ以外では不要
                        if dType not in agent:
                            check.pop('key_')
                        # SNMP v1 or v2以外では不要
                        if dType not in snmpV1_2:
                            check.pop('snmp_community', None)
                        # SNMP v3 以外では不要
                        if dType not in snmpV3:
                            snmpV3Param = [
                                'snmpv3_authpassphrase',
                                'snmpv3_authprotocol',
                                'snmpv3_contextname',
                                'snmpv3_privpassphrase',
                                'snmpv3_privprotocol',
                                'snmpv3_securitylevel',
                                'snmpv3_securityname',
                            ]
                            [check.pop(param, None) for param in snmpV3Param]
                        # ICMP以外では不要
                        if dType not in icmp:
                            # 7.0追加
                            check.pop('allow_redirect', None)
                items.append(item)
        except Exception as e:
            result = (False, 'processingDrule: %s' % e)
        self.STORE['drule'] = items
        return result

    def processingSla(self):
        '''
        ストアのslaデータを加工
        '''
        result = ZC_COMPLETE

        items = []
        deleteTarget = []
        try:
            for item in self.STORE.get('sla', []).copy():
                data = item['DATA']
                if self.checkMasterNode():
                    # マスターノード処理
                    pass
                else:
                    # ワーカーノード処理
                    # 空データの削除
                    [data.pop(param, None) for param in self.discardParameter['sla'] if not data.get(param)]
                items.append(item)
        except Exception as e:
            result = (False, 'processingSla: %s' % e)
        if not self.checkMasterNode():
            # ワーカー側削除対象
            names = [item['NAME'] for item in self.STORE.get('sla', [])]
            for name, item in self.LOCAL.get('sla', {}).items():
                if name not in names:
                    deleteTarget.append(item['ZABBIX_ID'])
        if items:
            self.STORE['sla'] = items
        if deleteTarget:
            self.STORE['slaExtend'] = [{'delete': deleteTarget}]
            self.sections['EXTEND'].append('slaExtend')
        return result

    def processingService(self):
        '''
        ストアのserviceデータを加工
        '''
        result = ZC_COMPLETE
        if not self.STORE.get('service'):
            return (True, 'No Data, service.')

        items= []
        extend = []
        deleteTarget = []
        try:
            for item in self.STORE.get('service', []).copy():
                name = item['NAME']
                data = item['DATA']
                if self.checkMasterNode():
                    # masterノード処理
                    # parents/childrenのnameの中身のみのリスト化
                    data['parents'] = [parent['name'] for parent in data['parents']]
                    data['children'] = [child['name'] for child in data['children']]
                else:
                    # ワーカーノード処理
                    # read-onlyの削除
                    [data.pop(param, None) for param in self.discardParameter['service']]
                    # サービス関連性の抜きだし
                    extend.append(
                        {
                            'NAME': name,
                            'DATA': {
                                'parents': [parent for parent in data.pop('parents', [])],
                                'children': [child for child in data.pop('children', [])]
                            }
                        }
                    )
                # 抜き出した後のデータ
                items.append(item)
        except Exception as e:
            result = (False, 'processingService: %s' % e)
        if not self.checkMasterNode():
            # ワーカー側削除対象
            names = [item['NAME'] for item in self.STORE.get('service', [])]
            for name, item in self.LOCAL.get('service', {}).items():
                if name not in names:
                    deleteTarget.append(item['ZABBIX_ID'])
        if items:
            self.STORE['service'] = items
        if extend or deleteTarget:
            self.sections['EXTEND'].append('serviceExtend')
            self.STORE['serviceExtend'] = []
        if extend:
            self.STORE['serviceExtend'].extend(extend)
        if deleteTarget:
            self.STORE['serviceExtend'].append({'delete': deleteTarget})
        return result

    def processingServiceExtend(self):
        '''
        Serviceの副処理
        Service同士の関係を処理processingServiceの後に設定
        Serviceが適用されてから実行しないとデータがない
        '''
        result = ZC_COMPLETE
        if not self.STORE.get('serviceExtend'):
            return (True, 'No Data, serviceExtend.')
        # マスターノードで行う処理はない（そもそもないからここを通らないはず）
        if self.checkMasterNode():
            return result

        items = []
        try:
            idName = self.getKeynameInMethod('service', 'id')
            for item in self.STORE['serviceExtend'].copy():
                if not item.get('delete'):
                    data = item['DATA']
                    # parents/childrenのID変換
                    children = data.get('children', [])
                    parents = (
                        [
                            {
                                idName: self.replaceIdName('service', parent)
                            } for parent in data.get('parents', [])
                        ]    
                    )
                    children = (
                        [
                            {
                                idName: self.replaceIdName('service', child)
                            } for child in data.get('children', [])
                        ]
                    )
                    data.update(
                        {
                            'parents': parents,
                            'children': children
                        }
                    )
                items.append(item)
        except Exception as e:
            result = (False, 'processingServiceExtend: %s' % e)
        self.STORE['serviceExtend'] = items
        return result

    def processingCorrelation(self):
        '''
        ストアのcorrelationデータを加工
        '''
        result = ZC_COMPLETE
        if not self.STORE.get('correlation'):
            return (True, 'No Data, correlation.')

        items = []
        try:
            for item in self.STORE['correlation'].copy():
                # 加工が必要なのはfilter内の項目のみ
                filter = item['DATA']['filter']
                # 読み取り専用削除
                filter.pop('eval_formula', None)
                # 不要項目の削除
                if int(filter['evaltype']) != 3:
                    # カスタム条件式以外では不要
                    filter.pop('formula', None)
                # 条件要素内の処理
                idName = self.getKeynameInMethod('hostgroup', 'id')
                for condition in filter['conditions'].copy():
                    # カスタム条件式以外では不要
                    if int(filter['evaltype']) != 3:
                        condition.pop('formulaid', None)
                    # ホストグループ対象（type == 2）のみID変換が必要
                    if int(condition['type']) == 2:
                        id = self.replaceIdName('hostgroup', condition[idName])
                        if id:
                            condition[idName] = id
                        else:
                            filter['conditions'].remove(condition)
                if len(filter['conditions']) == 0:
                    # 条件要素がすべてなくなってしまったものは削除
                    continue
                items.append(item)
        except Exception as e:
            result = (False, 'processingCorrelation: %s' % e)
        self.STORE['correlation'] = items
        return result

    def processingUser(self):
        '''
        ストアのuserデータを加工
        '''
        result = ZC_COMPLETE
        if not self.STORE.get('user'):
            return (True, 'No Data, user')
        
        items = []
        deleteTarget = []
        try:
            for item in self.STORE['user'].copy():
                data = item['DATA']
                # Media設定のID変換
                for media in data['medias'].copy():
                    # ID変換
                    idName = self.getKeynameInMethod('mediatype', 'id')
                    id = self.replaceIdName('mediatype', media[idName])
                    if id:
                        media.update({idName: id})
                    else:
                        # ID変換できないメディアは削除
                        data['medias'].remove(media)
                # 5.2 対応
                # ユーザー権限がtype -> roleになるのでID変換が必要になる
                if self.VERSION['major'] >= 5.2:
                    permitMethod = 'role'
                    permit = self.getKeynameInMethod(permitMethod, 'id')
                    data[permit] = self.replaceIdName(permitMethod, data.get(permit))
                    if not self.checkMasterNode() and self.getLatestVersion('MASTER_VERSION') < 5.2:
                        # 5.2以前は変換の必要がなかったので変換しないで代入
                        data[permit] = data.pop('type')
                else:
                    permit = 'type'
                usrgrps = 'usrgrps'
                if self.checkMasterNode():
                    # 所属Usergroup処理
                    # usrgrps:[]の中を{'name': 'xxxx'}のバリューだけにする
                    data[usrgrps] = [param['name'] for param in data.get(usrgrps, []) if param.get('name')]
                else:
                    # 認証サービスからの登録ユーザーは除外
                    if int(data.get('userdirectoryid', 0)):
                        continue
                    # 特権管理者の複製許可確認
                    if not self.CONFIG.cloningSuperAdmin:
                        if data[permit] == ZABBIX_SUPER_ROLE:
                            continue
                    # 複製許可ユーザーの確認
                    if self.getLatestVersion('MASTER_VERSION') >= 5.4:
                        idName = self.getKeynameInMethod('user', 'name')
                    else:
                        idName = 'alias'
                    password = self.CONFIG.enableUser.get(data[idName])
                    if not password:
                        continue
                    # パスワード設定を新規作成ユーザーに追加、既存ユーザーはパスワード変更はできない（元がわからない）
                    if item['NAME'] not in self.LOCAL['user'].keys():
                        data['passwd'] = password
                    # usrgrps:[]の中を{'usrgrpid': id}に変換する
                    idName = self.getKeynameInMethod('usergroup', 'id')
                    data[usrgrps] = [
                        {
                            idName: self.replaceIdName('usergroup', param)
                        } for param in data.get(usrgrps, [])
                    ]
                    # 不要項目削除
                    data.pop('userdirectoryid', None)
                    data.pop('users_status', None)
                    data.pop('gui_access', None)
                    data.pop('debug_mode', None)
                    medias = data.pop('medias', [])
                    addMedias = []
                    for media in medias.copy():
                        # 不要項目削除
                        media.pop('mediaid', None)
                        media.pop('userid', None)
                        # 7.0対応
                        if int(media.get('userdirectory_mediaid', 0)):
                            # 認証システムからの指定登録メディアは除外
                            medias.pop(media)
                            continue
                        media.pop('userdirectory_mediaid', None)
                        addMedias.append(media)
                    if addMedias:
                        if self.VERSION['major'] >= 5.2:
                            data['medias'] = addMedias
                        else:
                            data['user_medias'] = addMedias
                items.append(item)
        except Exception as e:
            return (False, 'processingUser: %s' % e)
        if not self.checkMasterNode():
            name = self.getKeynameInMethod('user', 'name')
            # ワーカー側削除対象
            users = [item['NAME'] for item in self.STORE.get('user', [])]
            for user, item in self.LOCAL.get('user', {}).items():
                if item['DATA'][name] == ZABBIX_SUPER_USER:
                    # Adminはスキップ
                    continue
                if user not in users:
                    deleteTarget.append(item['ZABBIX_ID'])
        self.STORE['user'] = items
        if deleteTarget:
            self.STORE['userExtend'] = [{'delete': deleteTarget}]
            self.sections['EXTEND'].append('userExtend')
        return result

    def processingUsergroup(self):
        '''
        ストアのusergroupデータを加工
        '''
        result = ZC_COMPLETE
        if not self.STORE.get('usergroup'):
            return (True, 'No Data, usergroup')

        items = []
        try:
            for item in self.STORE['usergroup'].copy():
                data = item['DATA']
                # 共通
                # tag_filtersのgroupidを変換
                idName = self.getKeynameInMethod('hostgroup', 'id')
                for tag in data.get('tag_filters', []):
                    [
                        tag.update(
                            {
                                idName: self.replaceIdName('hostgroup', tag[idName])
                            }
                        )
                    ]
                # rightsのidを変換
                # 6.2対応
                if self.VERSION['major'] >= 6.2:
                    targets = ['hostgroup', 'templategroup']
                else:
                    targets = ['']
                for target in targets:
                    rKey = '_'.join([target, 'rights']).lstrip('_')
                    if self.checkMasterNode:
                        pass
                    else:
                        if self.getLatestVersion('MASTER_VERSION') < 6.2:
                            # ワーカーノード処理
                            # マスターノードが6.2以前はrightsが分離されていないので*group_rightsにはrightsの内容を設定する
                            rKey = 'rights'
                    rights = data.get(rKey)
                    # 空ならスキップ
                    if not rights:
                        continue
                    # 初期化
                    data[rKey] = []
                    # 6.0以前の場合はNoneなのでhostgroupにする
                    if target is None:
                        target = 'hostgroup'
                    for val in rights:
                        # ID変換で値が返ってくるもののみを変換する
                        id = self.replaceIdName(target, val['id'])
                        if id:
                            data[rKey].append(
                                {
                                    'id': id,
                                    'permission': val['permission']
                                }
                            )
                if self.checkMasterNode():
                    # マスターノード処理
                    pass
                else:
                    # ワーカーノード処理
                    # 6.2対応
                    if self.VERSION['major'] >= 6.2:
                        # 0の場合不要
                        if not int(data.get('userdirectoryid', 0)):
                            data.pop('userdirectoryid', None)
                        # 内部認証（１）とフロントエンドアクセス禁止（３）では不要
                        if int(data.get('gui_access')) in [1, 3]:
                            data.pop('userdirectoryid', None)
                    # 7.0対応
                    if self.VERSION['major'] >= 7.0:
                        # MFAを使わないのであれば不要
                        if not data.get('mfa_status'):
                            data.pop('mfa_status', None)
                            data.pop('mfaid', None)
                    # 所属するユーザーのリストはusergroupには要らない（userの方で処理される）
                    data.pop('users', None)
                    data.pop('userids', None)
                    if not data.get('tag_filters'):
                        # 空の場合は項目を消す
                        data.pop('tag_filters', None)
                items.append(item)
        except Exception as e:
            return (False, 'processingUsergroup: %s' % e)
        self.STORE['usergroup'] = items
        return result

    def processingRole(self):
        '''
        ストアのroleデータを加工
        '''
        result = ZC_COMPLETE
        if not self.STORE.get('role'):
            return (True, 'No Data, role')
        
        items = []
        try:
            for item in self.STORE['role'].copy():
                data = item['DATA']
                if self.checkMasterNode():
                    # マスターノード処理
                    pass
                else:
                    # ワーカーノード処理
                    # 不要項目を削除
                    for param in data.copy().keys():
                        if param in self.discardParameter['role']:
                            data.pop(param, None)
                    rules = data['rules']
                    for rule, params in rules.copy().items():
                        if rule in self.discardParameter['role']:
                            rules.pop(rule)
                            continue
                        if isinstance(params, list):
                            for param in params:
                                if param.get('name') in self.discardParameter['role']:
                                    rules[rule].remove(param)
                    if self.VERSION['major'] >= 6.4:
                        # configuration.actionsの分割
                        value = 0
                        for param in data['rules']['ui'].copy():
                            if param.get('name') == 'configuration.actions':
                                value = int(param['status'])
                                data['rules']['ui'].remove(param)
                        if value and self.getLatestVersion('MASTER_VERSION') < 6.4:
                            data['rules']['ui'].extend(
                                [
                                    {'name': 'configuration.trigger_actions', 'status': value},
                                    {'name': 'configuration.service_actions', 'status': value},
                                    {'name': 'configuration.discovery_actions', 'status': value},
                                    {'name': 'configuration.autoregistration_actions', 'status': value},
                                    {'name': 'configuration.internal_actions', 'status': value},
                                ]
                            )
                    if self.CONFIG.zabbixCloud:
                        # ZabbixCloud対応: Module関連が存在しない
                        for param in self.zabbixCloudSpecialItem['role']:
                            item['DATA']['rules'].pop(param, None)
                items.append(item)
        except Exception as e:
            return (False, 'processingRole: %s' % e)
        self.STORE['role'] = items
        return result

    def processingUserdirectory(self):
        '''
        ストアのuserdirectoryデータを加工
        '''
        result = ZC_COMPLETE
        if not self.STORE.get('userdirectory'):
            return (True, 'No Data, userdirectory')
        
        items = []
        try:
            for item in self.STORE['userdirectory'].copy():
                data = item['DATA']
                # JITプロビジョンのメディアとユーザーグループ割り当てのID変換
                if data.get('provison_media'):
                    idName = self.getKeynameInMethod('meidatype', 'id')
                    for provMedia in data['provision_media'].copy():
                        # 不要項目の削除
                        provMedia.pop('userdirectory_mediaid', None)
                        id = self.replaceIdName('meidatype', provMedia[idName])
                        # メディアが存在しなかったら削除
                        if id:
                            provMedia[idName] = id
                        else:
                            data['provision_media'].pop(provMedia, None)
                if data.get('provision_groups'):
                    for provUgroup in data['provision_groups'].copy():
                        # roleのID変換
                        idName = self.getKeynameInMethod('role', 'id')
                        provUgroup['roleid'] = self.replaceIdName('role', provUgroup['roleid'])
                        # usergroupのID変換
                        idName = self.getKeynameInMethod('usergroup', 'id')
                        for ugrp in provUgroup['user_group'].copy():
                            id = self.replaceIdName('usergroup', ugrp[idName])
                            if not id:
                                provUgroup['user_group'].pop(ugrp, None)
                                continue
                            else:
                                ugrp[idName] = id
                        # ユーザーグループが空になっていたら設定内リストから削除
                        if len(provUgroup['user_group']) == 0:
                            data['provision_groups'].remove(provUgroup)
                if self.checkMasterNode():
                    # マスターノード処理
                    pass
                else:
                    # ワーカーノード処理
                    # 割り当てメディア設定が空になっていたら削除
                    if not data.get('provison_media'):
                        data.pop('provison_media', None)
                    # 割り当てグループ設定が空になっていたら削除
                    if not data.get('provision_groups'):
                        data.pop('provision_groups', None)
                items.append(item)
        except Exception as e:
            return (False, 'processingUserdirectory: %s' % e)
        self.STORE['userdirectory'] = items
        return result

    def processingMfa(self):
        '''
        ストアのMFAデータを加工
        '''
        result = ZC_COMPLETE
        if not self.STORE.get('mfa'):
            return (True, 'No Data, mfa')
        
        items = []
        try:
            for item in self.STORE['mfa'].copy():
                data = item['DATA']
                mfaType = int(data['type'])
                if self.checkMasterNode():
                    # マスターノード処理
                    pass
                else:
                    # ワーカーノード処理
                    # typeごとに不要な要素は削除
                    if mfaType == 1:
                        # TOTP
                        data.pop('api_hostname', None)
                        data.pop('clientid', None)
                        data.pop('client_secret', None) # ないはずだけど一応
                    elif mfaType == 2:
                        # Duo Universal Prompt
                        data.pop('hash_function', None)
                        data.pop('code_length', None)
                        # Duo Universal Promptのシークレットは設定から読み込む
                        name = data[self.getKeynameInMethod('mfa', 'name')]
                        secret = self.CONFIG.mfaClientSecret.get(name)
                        if secret:
                            data['client_secret'] = secret
                        else:
                            continue
                    else:
                        continue
                items.append(item)
        except Exception as e:
            return (False, 'processingMfa: %s' % e)
        self.STORE['mfa'] = items
        return result

    def processingAuthentication(self):
        '''
        ストアのAuthenticationデータを加工
        マスターノードのみ、ID変換処理をここで行う
        適用はsetAuthenticationToZabbix()で行う
        例外的な処理はあんまりやりたくないけどしゃあない
        '''
        if not self.STORE.get('authentication'):
            return (True, 'No Data, authentication')
        
        for item in self.STORE['authentication']:
            data = item['DATA']
            if item['NAME'] == 'disabled_usrgrpid':
                # LDAP/SAMLで使うdisabled_usrgrpidのID変換
                id = self.replaceIdName('usergroup', data['disabled_usrgrpid'])
                if id:
                    data['disabled_usrgrpid'] = id
            elif item['NAME'] == 'mfaid':
                # MFAのデフォルト利用のID変換
                id = self.replaceIdName('mfa', data['mfaid'])
                if id:
                    data['mfaid'] = id
            else:
                pass

        return ZC_COMPLETE

    def getDataFromZabbix(self):
        '''
        実行ノードのZabbixからデータを取得しLOCALに適用
        '''
        result = ZC_COMPLETE
        try:
            # メソッドIDと名前を取得
            for method, options in self.methodParameters.items():
                # メソッドが追加されたバージョン未満ならスキップ
                for version, addMethods in self.addMethods.items():
                    if self.VERSION['major'] < version and method in addMethods:
                        continue
                # 消えたメソッドはsuper().__init__でmethodParametersから削除されるので処理はない
                # methodParamterに登録されているメソッドのデータをget
                self.LOCAL[method] = {}
                getData = getattr(self.ZAPI, method).get(**options.get('options', {}))
                if method in self.sections['GLOBAL']:
                    # IDもNAMEもないので特別処理
                    id = 0
                    for key, value in getData.items():
                        self.LOCAL[method][key] = {
                            'ZABBIX_ID': id,
                            'NAME': key,
                            'DATA': {key: value}
                        }
                        id += 1
                else:
                    for data in getData:
                        # メソッドIDはZabbixがオブジェクト生成時に自動でつけるため、
                        # create時にワーカー側で邪魔になるのでDATAから取り出してZABBIX_IDに入れる
                        self.LOCAL[method][data[options['name']]] = {
                                'ZABBIX_ID': int(data.pop(options['id'])),
                                'NAME': data[options['name']],
                                'DATA': data
                        }
        except Exception as e:
            result = (False, 'Failed, getDataFromZabbix/API %s. %s' % (method, e))

        # 6.0以前のマスターノードならばデータベース操作でデータ取得
        if self.checkMasterNode and self.VERSION['major'] < 6.0:
            try:
                self.LOCAL['database'] = {}
                for table in self.sections['DB_DIRECT']:
                    res = self.getDbData(table)
                    if res[0]:
                        self.LOCAL['database'][table] = {
                            'ZABBIX_ID': None,
                            'NAME': table,
                            'DATA': res[1]
                        }
            except Exception as e:
                result = (False, 'Failed, getDataFromZabbix/DBDirect. %s' % e)

        # IDREPLACE: ZCを実行しているノードのZabbixから取得した値からの生成
        IDREPLACE = {}
        try:
            for method, data in self.LOCAL.items():
                IDREPLACE[method] = {}
                for item in data.values():
                    # ZABBIX_IDとNAMEがあるものだけ処理
                    if item.get('ZABBIX_ID') and item.get('NAME'):
                        IDREPLACE[method][item['ZABBIX_ID']] = item['NAME']
                        IDREPLACE[method][item['NAME']] = item['ZABBIX_ID']
            self.IDREPLACE = IDREPLACE
        except Exception as e:
            result = (False, 'Failed, getDataFromZabbix/IDREPLACE. %s' % e)

        return result

    def getConfigurationFromZabbix(self):
        '''
        通常のメソッドで取得すると、取得するためのパラメータのバージョン間変更対応が煩雑なため、
        configuration.export()で取れるものはこっちでデータを取得する
        '''
        # 取得対象のIDを抽出
        exportIds = {}
        templateIds=[]
        convSectionToMethod = {}
        for method, section in self.sections['CONFIG_EXPORT'].items():
            # option->methodの逆引き辞書を作る
            convSectionToMethod.update({section: method})
            if method == 'trigger':
                # トリガーの指定は不要なのでパスする
                continue
            items = [item['ZABBIX_ID'] for item in self.LOCAL[method].values()]
            if method == 'template':
                if self.CONFIG.templateSkip:
                    continue
                templateIds = items
            else:
                exportIds.update({section: items})
        
        exportIds = [exportIds]

        # 負荷対策
        # テンプレートはZC_TEMPLATE_SEPARATEごとに分割して別処理
        start = loop = 0
        while len(templateIds) > start:
            loop += 1
            count = self.CONFIG.templateSeparate * loop
            exportIds.append({'templates': templateIds[start:count]})
            start = count

        # configuration.export()の実行、JSONに変換
        exportData = []
        for item in exportIds:
            try:
                data = self.ZAPI.configuration.export(
                    **{
                        'format': 'json',
                        'options': item
                    }
                )
                # mediatype表記ゆれ対応: 出力のmedia_types（ここでしか出てこない） -> importOption/ExportのmediaTypesに変換 
                exportData.append(json.loads(data.replace('media_types', 'mediaTypes')).get('zabbix_export'))
            except Exception as e:
                return (False, 'configuration export, Failed, %s' % e)

        for data in exportData:
            # configurationから不要データを取り除いて成型
            for section, items in data.copy().items():
                # セクション名からメソッド名を引く
                method = convSectionToMethod.get(section, None)
                if not method:
                    # メソッドを引けないものを排除（version/date）
                    data.pop(section)
                    continue
                # LOCALにmethodがなかったら初期化（今のところtriggerくらいのはず）
                if not self.LOCAL.get(method):
                    self.LOCAL[method] = {}
                # マスターノードからの取得処理でない場合にID<->Name変換テーブルのmethodがなければ初期化
                if not self.IDREPLACE.get(method):
                    self.IDREPLACE[method] = {}
                # name要素ルールの例外処理
                if method in ['trigger']:
                    name = None
                else:
                    name = self.getKeynameInMethod(method, 'name')
                # 例外処理用の連番
                id = 0
                # トリガーはテンプレートの影響で分割されてくるので、現在の最大を取得する
                if method == 'trigger':
                    id = [val['ZABBIX_ID'] for val in self.LOCAL['trigger'].values()]
                    id = max(id) + 1 if id else 0                        
                # LOCALに適用
                for item in items:
                    # 例外処理の場合はmethod+idを名前にする
                    itemName = item.get(name)
                    if not itemName:
                        itemName = item.get('uuid', method + str(id))
                        self.LOCAL[method][itemName] = {}
                    self.LOCAL[method][itemName].update(
                        {
                            'NAME': itemName,
                            'DATA': item
                        }
                    )
                    # LOCALに入るはずのデータでZABBIX_IDがないものにidを入れる
                    zId = self.LOCAL[method][itemName].get('ZABBIX_ID', None)
                    if not zId:
                        self.LOCAL[method][itemName]['ZABBIX_ID'] = id
                        # id<->name変換テーブルに追加する
                        self.IDREPLACE[method].update(
                            {
                                id: itemName,
                                itemName: id,
                            }
                        )
                    # カウントアップ
                    id += 1

        return ZC_COMPLETE

    def getDataFromMaster(self, master):
        '''
        ストア:Directのマスターノードからのデータ読み込み
        '''
        master.VERSIONS = self.VERSIONS
        master.VERSIONS[0].update(
            {
                'MASTER_VERSION': master.VERSION['major'],
            }
        )

        # マスターノードからダイレクトにデータを取得する
        if master.CONFIG.directMaster:
            # 接続先のサーバー名確認
            result = CHECK_ZABBIX_SERVER_NAME(master.CONFIG.endpoint, master.CONFIG.node)
            if result[0]:
                # マスター側の取得
                result = getattr(master, 'getDataFromZabbix')()
            if result[0]:
                # マスター側のデータ取得
                result = getattr(master, 'createNewData')()
            return result
        else:
            return (False, 'Not Master-Node.')

    def getDataFromStore(self, **params):
        '''
        データストアからデータを取得する
        version: 対象のバージョン、なければ最新
        '''
        # マスターノードからダイレクトにデータを取得する
        if params.get('master'):
            master = params['master']
            if not isinstance(master, ZabbixClone):
                return (False, 'Not Master Instance.')
            result = self.getDataFromMaster(master)
            if result[0]:
                self.STORE = master.STORE
                self.VERSIONS = master.VERSIONS
                return ZC_COMPLETE
            else:
                return result
        
        # ストアからの読み込みここから
        result = ZC_COMPLETE

        # 基本最新版を使用、ワーカーノードでバージョン指定があり、ストアにある場合はそれを使う
        # CONFIGの時点でマスターではNoneになってる
        version = [item for item in self.VERSIONS if item['VERSION_ID'] == self.CONFIG.targetVersion]
        if len(version)!= 1:
            version = self.getLatestVersion()
        else:
            version = version[0]
        # 継承元クラスの同名ファンクションを使ってストアからデータを取得
        result = super().getDataFromStore(version)
        if not result[0]:
            return result
        # ファイルの場合
        if self.CONFIG.storeType == 'file':
            return result
        # ローカルで使う形に成型
        for item in result[1]:
            method = item.get('METHOD')
            # METHODがないので不正データ
            if not method:
                return (False, 'wrong data from getDataFromStore, %s' % version)
            # self.STOREにMETHODがなければ初期化
            if not self.STORE.get(method):
                self.STORE[method] = []
            try:
                # 適用、データが足りていなければ失敗
                self.STORE[method].append(
                    {
                        'NAME': item['NAME'],
                        'DATA': item['DATA'],
                        'DATA_ID': item['DATA_ID'],
                    }
                )
            except:
                return (False, 'Not enough data: %s' % json.dumps(item))
        return ZC_COMPLETE

    def setVersionDataToStore(self):
        '''
        self.STOREの内容をストアにアップロードする
        アップロード対象はVERSIONとDATA
        '''
        # バージョン情報の新規生成
        self.createNewVersion()

        # DATA
        # 引数は{method: [item,item,...],}
        # ストアへの適用実行
        result = self.setDataToStore(self.NEW)
        if not result[0]:
            return result

        # ファイル出力の場合は終了
        if self.CONFIG.storeType == 'file':
            self.NEW.pop('DESCRIPTION', None)
            return (True, self.NEW)

        # VERSION
        # データが成功してからバージョンを入れる
        # 引数は**{'VERSION_ID': xxx, 'UNIXTIME': 000000000, 'MASTER_VERSION': 'x.x', 'DESCRIPTION': ''}
        result = self.setVersionToStore(**self.NEW)
        if not result[0]:
            return result

        return (True, self.NEW)

    def setGlobalsettingsToZabbix(self):
        '''
        グローバル設定／正規表現の適用
        6.0以降はAPIで対応
        '''
        result = ZC_COMPLETE

        # マスターではこのファンクションは実行不要
        if self.checkMasterNode():
            return (False, 'Not Execute with master-node.')
 
        if self.VERSION['major'] >= 6.0:
            # 6.0以降はAPIで設定
            # グローバル設定
            globalSettings = {}
            for item in self.STORE.get('settings', []):
                if item['NAME'] in self.discardParameter['settings']:
                    continue
                globalSettings.update(item['DATA'])

            if self.CONFIG.settings:
                # 重要度文言設定の読み込み
                for lv, sev in self.CONFIG.settings.get('severity', {}).items():
                    if sev.get('name'):
                        globalSettings.update({'severity_name_' + lv: sev['name']})
                    if sev.get('color') and int(sev['color'], 16):
                        globalSettings.update({'severity_color_' + lv: sev['color']})

                # 7.0以降のタイムアウト設定の読み込み
                if self.VERSION['major'] >= 7.0:
                    for target, value in self.CONFIG.settings.get('timeout', ZC_TIMEOUT_LOWER).items():
                        target.removeprefix('timeout_')
                        # TIMEOUTの対象か確認
                        if target not in self.timeoutTarget:
                            continue
                        value = str(value)
                        # SUFFIX外す
                        if 's' in value:
                            value.rstrip('s')
                            suffix = 's'
                        elif 'm' in value:
                            value.rstrip('m')
                            suffix = 'm'
                        else:
                            # 数字じゃない場合は無視（hとかdとか入れてるのを想定）
                            if not value.isdigit():
                                continue
                            suffix = 's'
                        value = int(value)
                        # 分は秒に直す
                        if suffix == 'm':
                            value = value * 60
                        # 制限範囲の確認
                        if value < 1:
                            # Zabbix仕様の下限1秒
                            value = 1
                        elif value > 600:
                            # Zabbix仕様の上限600秒
                            value = 600
                        # ZCにおける下限指定
                        if ZC_TIMEOUT_LOWER.get(target) and value < ZC_TIMEOUT_LOWER[target]:
                            value = ZC_TIMEOUT_LOWER[target]
                        globalSettings.update({f'timeout_{target}': f'{value}s'})

            # settingsの適用
            if globalSettings:
                try:
                    self.ZAPI.settings.update(**globalSettings)
                except Exception as e:
                    result = (False, 'Failed, Settings/update. %s' % e)

            # secret globamacroの追加 secretが5.0以降なので一応確認
            if result and self.VERSION['major'] >= 5.0:
                for item in self.CONFIG.secretGlobalmacro:
                    try:
                        # 必要項目があるか確認も込みでgetを使わない
                        macro = {
                            'macro': item['macro'],
                            'value': item['value'],
                            'type': 1
                        }
                        self.ZAPI.usermacro.createglobal(**macro)
                    except Exception as e:
                        result = (False, 'Failed, Secret Globalmacro/create %s. %s' % (item, e))

        else:
            # 6.0以前のDB Direct操作
            for tableData in self.STORE.get('database', []):
                if not tableData:
                    # 入れ替えるデータがない
                    result = (True, 'No Exist DB_DIRECT data.')
                table = tableData['NAME']
                data = tableData['DATA']
                if table == 'config':
                    # col名変更対応
                    for ver, renames in self.dbConfigRenameCols.items():
                        # 自身のバージョンが適用されたバージョンより新しければカラムの名前を変える
                        if self.VERSION['major'] >= float(ver):
                            for rename in renames:
                                try:
                                    idx = data[0].index(rename[0])
                                    value = data[1][idx]
                                    del data[0][idx]
                                    del data[1][idx]
                                    data[0].append(rename[1])
                                    data[1].append(value)
                                except:
                                    pass
                    # 廃止されたColのデータ削除
                    for version, drops in self.dbConfigDropCols.items():
                        # 自身のバージョンが適用されたバージョンより新しければカラムを削除する
                        if self.VERSION['major'] >= version:
                            for drop in drops:
                                try:
                                    idx = data[0].index(drop)
                                    del data[0][idx]
                                    del data[1][idx]
                                except:
                                    pass
                    result = self.updateDbData(table, data)
                    if not result[0]:
                        break
                else:
                    result = self.replaceDbData(table, data)
                    if not result[0]:
                        break
        return result

    def setConfigurationToZabbix(self):
        '''
        STOREからZabbixインポートデータの生成、適用
        CONFIG_IMPORTセクション
        '''

        # バージョン対応のメソッド-セクション対応dictの生成
        sections = {}
        for masterVersion, imports in self.sections['CONFIG_IMPORT'].items():
            # 適用するバージョンより処理バージョンの方が大きければ必要なし
            if masterVersion > self.getLatestVersion('MASTER_VERSION'):
                continue
            else:
                for section, method in imports.items():
                    sections[method] = section

        # データ作成
        importData = {}
        for method, section in sections.items():
            data = self.STORE.get(method)
            if not data:
                continue
            if method == 'trigger':
                continue
            elif method == 'host':
                # hostは並列処理createで入れるのでスキップ、項目は必要
                importData[section] = []
            elif method == 'template':
                templates = []
                # 6.4 HTTP_AGENT以外に入っている「request_method: POST」を削除
                for item in data:
                    template = item['DATA']
                    if template.get('items'):
                        # 通常アイテム
                        for item in template['items']:
                            if item.get('type') != 'HTTP_AGENT':
                                item.pop('request_method', None)
                    if template.get('discovery_rules'):
                        # LLD
                        for rule in template.get('discovery_rules', []):
                            # LLDのアイテム
                            if rule.get('type') != 'HTTP_AGENT':
                                rule.pop('request_method', None)
                            if rule.get('item_prototypes'):
                                # アイテムのプロトタイプ
                                for item in rule['item_prototypes']:
                                    if item.get('type') != 'HTTP_AGENT':
                                        item.pop('request_method', None)
                    templates.append(template)
                templates = sorted(templates, key=lambda x:x['name'])
            elif method == 'mediatype':
                for item in data:
                    mediatype = item['DATA']
                    if self.VERSION['major'] >= 6.0:
                        # 6.0対応 content-type入りだと失敗するので削除
                        if mediatype.get('type') == 'SCRIPT':
                            mediatype.pop('content_type', None)
                    if self.VERSION['major'] >= 6.4:
                        # 6.4対応 SCRIPTが順序データ入りになった
                        if mediatype.get('type') == 'SCRIPT':
                            idx = 0
                            params = []
                            for param in mediatype.get('parameters', []):
                                if isinstance(param, str):
                                    params.append({'sortorder': str(idx), 'value': param})
                                else:
                                    if param.get('sortorder') and param.get('value'):
                                        params.append(param)
                                idx += 1
                            mediatype.update({'parameters': params})
                    if self.VERSION['major'] >= 7.0:
                        # 7.0 content_type完全廃止
                        mediatype.pop('content_type', None)
                # mediatypeの表記ゆれ対応

                importData['media_types'] = [item['DATA'] for item in data]
            else:
                importData[section] = [item['DATA'] for item in data]

        importData = [importData]
        triggers = [trigger['DATA'] for trigger in self.STORE.get('trigger', [])] 

        # テンプレートの分割処理
        templateGroup = []
        group = 0
        groups = {}
        processed = []
        while templates:
            groups[group] = []
            # グループ０：リンクするテンプレートのない
            # グループ１：グループ０のみリンクしている
            # グループ２：グループ０，１をリンクしている
            # …前グループをリンクしているものがなくなるまで繰り返して分類
            for template in templates.copy():
                # 6.0以前のテンプレートのグループ対応
                if template.get('groups'):
                    templateGroup.extend(template['groups'])
                # ホストのプロトタイプのテンプレートを確認し、processedになければ飛ばす
                ptypeTemplate = []
                for lld in template.get('discovery_rules', []):
                    for ptype in lld.get('host_prototypes', []):
                        ptypeTemplate.extend([item['name'] for item in ptype.get('templates', [])])
                set(ptypeTemplate)
                if not LISTA_ALL_IN_LISTB(ptypeTemplate, processed):
                    continue
                # リンクしているテンプレートが処理済みリストにない
                links = [link['name'] for link in template.get('templates', [])]
                if LISTA_ALL_IN_LISTB(links, processed):
                    # groupに追加
                    groups[group].append(template)
                    # 元リストから消す
                    templates.remove(template)
            # 処理済みに追加
            name = self.getKeynameInMethod('template', 'name')
            processed.extend([template[name] for template in groups[group]])
            # 次のグループ
            group += 1
        # さらにそれぞれのグループをZC_TEMPLATE_SEPARATEずつ分離してimportDataに追加
        # 一応0から順にソートする
        count = 0
        for group in sorted(groups.keys()):
            items = groups[group]
            # インポートエラーが一つでも出ると全部巻き込まれるので、１つずつ入れることにした
            count = 0
            while len(items) > count:
                if self.VERSION['major'] in [6.0, 7.0]:
                    # 6.0/7.0だとこっちで依存関係のは問題なく全部入る
                    # なんで？？？？
                    iData = {
                        'templates': [items[count]],
                        'triggers': triggers
                    }
                    # マスターのバージョンが6.0未満だとvalue_mapsがtemplatesに入ってないので必要
                    if self.getLatestVersion('MASTER_VERSION') < 6.0:
                        iData['value_maps'] = importData[0].get('value_maps')
                    importData.append(iData)
                elif self.VERSION['major'] == 5.4:
                    # 5.0はvaluemapが別になっているので5.4はテンプレート側処理時に必要になる
                    self.importRules['triggers']['createMissing'] = False
                    importData.append(
                        {
                            'templates': [items[count]],
                            'value_maps': importData[0]['value_maps']
                        }
                    )
                else:
                    # trigger prototype内の依存関係以外はこれで入る
                    self.importRules['triggers']['createMissing'] = False
                    importData.append({'templates': [items[count]]})
                count += 1

        # マスターのバージョンが6.2未満でノード側が6.2以上の場合
        if self.getLatestVersion('MASTER_VERSION') < 6.2 and self.VERSION['major'] >= 6.2:
            templateGroup = [item['name'] for item in templateGroup]
            templateGroup = set(sorted(templateGroup))
            # ホストグループの内templateGroupにあるものは除外
            groups = []
            for group in importData[0]['groups'].copy():
                if group['name'] in templateGroup:
                    continue
                groups.append(group)
            importData[0]['groups'] = groups
            for item in templateGroup:
                if item in self.LOCAL['templategroup'].keys():
                    continue
                try:
                    self.ZAPI.templategroup.create(**{'name': item})
                except Exception as e:
                    print(e)
                    return (False, 'Failed Convert Hostgroup before 6.2 -> Templategroup 6.2 or later. %s. %s' % (item, e))

        # インポートデータ処理
        # テンプレートとホスト以外を全部処理、次にテンプレートをZC_TEMPLATE_SEPARATEずつ処理、ホストは次のファンクション
        # 表示（仮）
        print(f'\n{TAB*2}Template Import:\n{TAB*3}', end='', flush=True)
        if self.CONFIG.templateSkip:
            print('SKIP.')
            
        # 表示（仮）
        dispRow = 1
        templateResult = {'success': 0, 'failed': 0, 'messages': []}
        skip = False
        for importItems in importData:
            if skip and self.CONFIG.templateSkip:
                continue
            skip = True
            # テンプレート用処理
            if 'templates' in importItems.keys():
                # 処理するテンプレートの名前
                templateProcess = importItems['templates'][0]['name']
            else:
                templateProcess = None
            importItems.update(
                {
                    'version': str(self.getLatestVersion('MASTER_VERSION')),
                    'date': ZABBIX_TIME()
                }
            )
            # 7.0対応
            if self.getLatestVersion('MASTER_VERSION') >= 7.0:
                importItems.pop('date', None)
            # インポート内容のJSONテキスト化
            try:
                importItems = '{"zabbix_export":%s}' % json.dumps(importItems, ensure_ascii=False)
            except:
                return (False, 'Failed Convert ImportFile: %s' % self.getLatestVersion('VERSION_ID'))
            # インポート実行
            try:
                result = self.ZAPI.configuration['import'](
                    **{
                        'format': 'json',
                        'rules': self.importRules,
                        'source': importItems,
                    }
                )
                if templateProcess:
                    # テンプレートを処理している場合の結果
                    if not result:
                        templateResult['failed'] += 1
                        templateResult['message'].append(
                            {
                                'name': templateProcess,
                                'error': 'Failed, No Result return.'
                            }
                        )
                        # 表示（仮）
                        print('F', end='', flush=True)
                    else:
                        templateResult['success'] += 1
                        # 表示（仮）
                        print('.', end='', flush=True)
            except Exception as e:
                if templateProcess:
                    templateResult['failed'] += 1
                    templateResult['messages'].append(
                        {
                            'name': templateProcess,
                            'error': e
                        }
                    )
                    # 表示（仮）
                    print('X', end='', flush=True)
                else:
                    # テンプレート以外の失敗は即終了
                    return (False, 'Failed Execute Import. %s' % e)
                
            # テンプレートの実行中の改行
            # 表示（仮）
            dispCount = T_COUNT*3*dispRow + templateResult['success'] + templateResult['failed']
            if templateProcess and dispCount == WIDE_COUNT * dispRow:
                print(f'\n{TAB*3}', end='', flush=True)
                dispRow += 1

        # テンプレートインポートの結果
        # 表示（仮）
        if len(importData) > 1:
            print('\n%sSuccess:%s / Failed:%s' % (TAB*3, templateResult['success'], templateResult['failed']))
            if templateResult['messages']:
                print(f'{TAB*3}Import Error\'s Message:')
                for message in  templateResult['messages']:
                    print('%s%s: \n%s%s' % (TAB*4, message['name'], TAB*5, message['error']))

        # テンプレート適用したのでZabbixからデータを取得、IDREPLACEの更新
        result = self.getDataFromZabbix()
        if not result[0]:
            return result

        return ZC_COMPLETE

    def setApiToZabbix(self, section):
        '''
        STOREからAPIでZabbixにデータを適用する
        API/REPLACEセクション
        '''
        # 6.0以降対応
        if section == 'GLOBAL':
            # 一般設定系は形式が違うのでここで実行できない
            return (False, 'Cannot Execute GLOBAL sections')

        # セクション内に何もないか、そもそもセクションがない
        if not self.sections.get(section):
            return (True, f'{section} is Empty.')
        sections = self.sections[section]

        # 表示（仮）
        print(f'\n{TAB*2}Method Data Convert in {section} section:', flush=True)
        
        # EXTENDで削除する場合、適用の逆順でないといけない場合があるのでリバース
        # 適用: プロキシグループ -> プロキシ（プロキシグループのExtendが先にリストに入る）
        # 削除: プロキシ -> プロキシグループ（逆順にすることでプロキシが先に削除される）
        if section == 'EXTEND':
            sections = reversed(sections)

        # データの変換処理
        result = self.processingMethodData(section)
        if not result[0]:
            return result

        # 表示（仮）
        for row in result[1]:
            print(row)

        # セクションの適用
        # 表示（仮）
        print(f'\n{TAB*2}Execute API in {section} section:',end='', flush=True)

        for method in sections:
            items = []
            api = getattr(self.ZAPI, method.removesuffix('Extend'))
            # データ操作
            for item in self.STORE.get(method, []):
                if item.get('delete'):
                    # 一つずつ削除する形に変える
                    [items.append({'delete': delete}) for delete in item['delete']]
                else:
                    name = item['NAME']
                    data = item['DATA']
                    if method == 'serviceExtend':
                        # serviceの親子相関対応はserviceの付属情報なのでそちらのIDを使う
                        idName = self.getKeynameInMethod('service', 'id')
                        id = self.replaceIdName('service', name)
                        if id:
                            data.update({idName: id})
                            items.append({'update': data})
                    else:
                        if name in self.LOCAL[method].keys():
                            # LOCALにあるものはupdate、ZABBIX_IDをDATAの中に入れる
                            idName = self.getKeynameInMethod(method, 'id')
                            data[idName] = self.LOCAL[method][name]['ZABBIX_ID']
                            items.append({'update': data})
                        else:
                            # LOCALにないものはcreate
                            items.append({'create': data})
            
            # 表示（仮）
            print(f'\n{TAB*3}{method}: ',end='', flush=True)

            # 実行
            for item in items:
                if item.get('update'):
                    function = 'update'
                elif item.get('create'):
                    function = 'create'
                elif item.get('delete'):
                    function = 'delete'
                else:
                    continue
                item = item[function]
                # usermacroのグローバルマクロはファンクションにglobalがつくので加工
                if method == 'usermacro':
                    function += 'global'
                try:
                    if function == 'delete':
                        if self.CONFIG.noDelete:
                            continue
                        getattr(api, function)(item)
                        res = 'D'
                    else:
                        getattr(api, function)(**item)
                        res = 'C' if 'create' in function else 'U'
                except Exception as e:
                    res = 'X'                   
                    result = (False, 'setApiToZabbix, %s.%s, %s' % (method.removesuffix('Extend'), function, e))

                # 表示（仮）
                print(f'{res}', end='', flush=True)

                if not result[0]:
                    return result

        # API実行が終わったらローカルを更新
        self.getDataFromZabbix()

        # 表示（仮）
        print('')
    
        return ZC_COMPLETE

    def setHostToZabbix(self):
        '''
        STOREデータを加工し、Zabbixへhostを適用する
        hostsは数が多いのでAPIを並列処理する
        あとバージョン上がってデータ形式が変更すると下位バージョンのインポートファイルで
        エラーになる場合が多いのでデータ形式をここで変換する（キー名変わる可能性もあるしね）
        '''
        # Yes/Noの値変換
        Y_N = {'NO': 0, 'YES': 1}
        hosts = []
        for host in self.STORE['host']:
            name = host['NAME']
            data = host['DATA']
            # 適用可能ホストの判定:ZC_WORKERタグのバリューを利用する
            monitorNode = [tag.get('value') for tag in data.get('tags', []) if tag.get('tag') == ZC_MONITOR_TAG]
            hUuid = [tag.get('value') for tag in data.get('tags', []) if tag.get('tag') == ZC_UNIQUE_TAG]
            hUuid = hUuid[0] if hUuid else None
            if self.CONFIG.node in monitorNode and self.CONFIG.role == ZC_DERAULT_ROLE:
                # 監視有効で適用するホスト
                data.update({'status': ZABBIX_ENABLE})
            elif self.CONFIG.role in ZC_ALL_ALLOW_ROLE:
                # 監視無効で適用するホスト
                data.update({'status': ZABBIX_DISABLE})
            else:
                # 監視対象ではないので次の処理
                continue
            # ホスト直設定のアイテム、トリガー、LLDは除外
            [data.pop(section, None) for section in self.discardParameter['host']]
            # バリューなしのキーを削除:5.x系であったcreateの空データ無視がなくなった時の対応（だったかな）
            [data.pop(key, None) for key, value in data.copy().items() if not value]
            # インベントリモードの変換:MANUALの場合キーが存在しない
            data['inventory_mode'] = ZABBIX_INVENTORY_MODE.get(data['inventory_mode'], ZABBIX_INVENTORY_MODE['MANUAL'])
            # インターフェイスの処理
            if len(data['interfaces']) == 1:
                # インターフェイスが一つしかない場合はそれがメインインターフェイス
                data['interfaces'][0]['default'] = 'YES'
            for hostIf in data['interfaces']:
                # create時に不要なので削除
                hostIf.pop('interface_ref', '')
                ifType = hostIf.get('type', 'AGENT')
                hostIf.update(
                    {
                        'ip': hostIf.get('ip', '127.0.0.1'),
                        'main': Y_N[hostIf.pop('default', 'NO')],
                        'port': hostIf.get('port', '10050'),
                        'type': ZABBIX_IFTYPE[ifType],
                        'useip': 0 if hostIf.get('useip', 'YES') == 'NO' else 1,
                        'dns': hostIf.get('dns', ''),
                    }
                )
                # 強制DNS->IP変換処理
                if hostIf['useip'] == 0 and self.CONFIG.forceUseip:
                    try:
                        new_ip = socket.gethostbyname(hostIf['dns'])
                    except:
                        new_ip = '0.0.0.0'
                    if new_ip != '0.0.0.0':
                        hostIf['ip'] = new_ip
                        hostIf['useip'] = 1
                        hostIf.pop('dns', None)
                # 5.0対応
                if self.VERSION['major'] >= 5.0:
                    # bulkがdetailsの中に移動なので削除
                    hostIf.pop('bulk', None)
                    # SNMPは接続設定detailsが追加、他のインターフェイスはあっても無視される
                    if ifType == 'SNMP':
                        useVersion = hostIf['details'].get('version', 'SNMPV2').upper() if hostIf.get('details') else 'SNMPV2'
                        snmpCommunity = hostIf['details'].get('community', ZABBIX_SNMP_COMMUNITY)
                        hostIf.update(
                            {
                                'details': {
                                    'version': ZABBIX_SNMP_VERSION[useVersion],
                                    'community': snmpCommunity
                                }
                            }
                        )
                else:
                    hostIf['bulk'] = Y_N[hostIf.get('bulk', 'YES')]
            # Proxy変換
            if self.VERSION['major'] >= 7.0:
                if self.getLatestVersion('MASTER_VERSION') >= 7.0:
                    # 7.0対応 プロキシグループとの区別が追加
                    # 各所で表記ブレブレなのどうにかしてよ……
                    proxyType = data.pop('monitored_by', 'direct').lower()
                else:
                    proxyType = 'proxy'
                monitor = ZABBIX_PROXY_MODE.get(proxyType, 0)
                if monitor > 0:
                    # proxyの種類と対象を決定
                    proxy = data.pop(proxyType, None)
                    # プロキシ情報を追加
                    data.update(
                        {
                            'monitored_by': monitor,
                            proxyType + 'id': self.replaceIdName(proxyType.replace('_', ''), proxy['name'])
                        }
                    )
            else:
                proxy = data.pop('proxy', None)
                if proxy:
                    data['proxy_hostid'] = self.replaceIdName('proxy', proxy['name'])
            # テンプレートとホストグループのID変換
            for method in ['template', 'hostgroup']:
                section = method + 's'
                section = section.removeprefix('host')
                id = self.getKeynameInMethod(method, 'id')
                data[section] = [
                    {
                        id: self.replaceIdName(method, item['name'])
                    } for item in data.get(section, []) if item['name'] in self.LOCAL[method].keys()
                ]
            # 加工したデータをインポートリストに追加
            hosts.append(
                {
                    'name': name,
                    'data': data,
                    'uuid': hUuid,
                }
            )
        # create/update/deleteの決定
        # ローカルのホスト確認のUUIDテーブル生成
        # {'ZC_UUIDの中身': 'ローカルホストのhostid'}
        hostUuids = {}
        for item in self.LOCAL['host'].values():
            tags = [tag['value'] for tag in item['DATA']['tags'] if tag['tag'] == ZC_UNIQUE_TAG]
            if not tags:
                continue
            hostUuids.update(
                {
                    tags[0]: item['ZABBIX_ID']
                }
            )

        # create/update/delete判定と処理
        '''
        host.create/update実体
        {
            'name': ホスト名,
            'data': 加工済み適用データ,
            'uuid': ホスト同一性確認のUUID,
            'function': 'update/create'
        }
        '''
        # インターフェイスのアップデートは別にしないといけないっぽいので取り出す
        updateInterfaces = []
        for item in hosts.copy():
            localHost = self.LOCAL['host'].get(item['name'])
            idName = self.getKeynameInMethod('host', 'id')
            data = item['data']
            hostId = None
            if localHost:
                # 同じ名前のホストが既にある
                if item['uuid'] in hostUuids.keys():
                    # ZC_UUIDも同じならば更新
                    function = 'update'
                    # 更新対象のIDを入れる
                    hostId = localHost['ZABBIX_ID']
                    update = True
                else:
                    # UUIDが違うので別ホストの名前変更で衝突
                    if self.CONFIG.forceHostUpdate:
                        # 強制アップデートの場合はストアの情報でupdate実行
                        function = 'update'
                        # ローカルにあるホストのIDを使う
                        hostId = localHost['ZABBIX_ID']
                        update = True
                    else:
                        # 強制しない場合はインポート対象から削除
                        hosts.remove(item)
                        continue
                if update:
                    # インターフェイスの更新は別でやらないといけない
                    hostIfs = data.pop('interfaces', None)
                    if hostIfs:
                        updateInterfaces.append(
                            {
                                'host': item['name'],
                                'id': localHost['ZABBIX_ID'],
                                'data': hostIfs
                            }
                        )
            else:
                # 同じホスト名はない
                if item['uuid'] in hostUuids:
                    # ローカルで別のホスト名に変えていた
                    if self.CONFIG.forceHostUpdate:
                        # 強制アップデートの場合はローカルの名前でupdate実行
                        function = 'update'
                        # ストアの情報から名前を抜く
                        data.pop('host', '__NO_HOST.HOST__')
                        data.pop('name', '__NO_HOST.NAME__')
                        # ローカルにあるホストのIDを使う
                        hostId = hostUuids[item['uuid']]
                    else:
                        # 強制しない場合はインポート対象から削除
                        hosts.remove(item)
                        continue
                else:
                    # 完全に同じホストはいないのでそのままcreate
                    function = 'create'
            item['function'] = function
            data[idName] = hostId

        # 監視する対象がないので終了
        if not hosts:
            if self.CONFIG.forceHostUpdate:
                return (True, 'No Exist Monitoring Hosts with %s.' % self.CONFIG.node)
            else:
                return (True, 'Not Allowed Host Update, All Excluded.')


        # 並列処理の結果
        result = []
        # 並列処理中のホスト作成ファンクション
        def importHost(host):
            function = host['function']
            name = host['name']
            data = host['data']
            res = 'C' if function == 'create' else 'U'
            try:
                getattr(self.ZAPI.host, function)(**data)
                result = (True, function)
            except Exception as e:
                # hostはインポート失敗しても止めずに進める
                result = (False, function, name, e)
                res = 'X'
            # 表示（仮）
            print(f'{res}', end='', flush=True)
            return result

        # host.createの並列実行、実行数はphp-fpmのフォーク数以下にする
        # ZabbixのAPI応答ベースの処理なのでProcess*じゃなくてThread*を使ってる

        # 表示（仮）
        print(f'\n{TAB*2}Host Import:\n{TAB*3}', end='', flush=True)

        future_list = []
        with futures.ThreadPoolExecutor(max_workers=self.CONFIG.phpWorkerNum) as executor:
            for host in hosts:
                future = executor.submit(importHost, host)
                future_list.append(future)
        futures.as_completed(fs=future_list)

        result = [item._result for item in future_list]
        create = len([item for item in result if item[0] and item[1] == 'create'])
        update = len([item for item in result if item[0] and item[1] == 'update'])
        failed = [item for item in result if not item[0]]

        # 表示（仮）
        print(f'\n{TAB*3}Create: {create} / Update: {update}', end='', flush=True)

        if failed:
            print(f' / Failed: {len(failed)}')
            print(f'{TAB*3}Failed Hosts:', end='', flush=True)
            for item in failed:
                print(f'\n{TAB*4}{item[2]}: {item[3]}')
        else:
            print('')

        # インターフェイスのアップデート
        deleteInterfaces = []
        # 表示（仮）
        display = []
        if updateInterfaces:

            dispCount = T_COUNT*3
            print(f'\n{TAB*2}Update Interface(s):\n{TAB*3}',end='', flush=True)

            idName = self.getKeynameInMethod('host', 'id')
            for updateIfs in updateInterfaces:

                hostId = updateIfs['id']
                hostName = updateIfs['host']
                
                # 表示（仮）
                disp = f'{TAB*3}{hostName}: '

                try:
                    # インターフェイスの取得
                    hostIfs = self.ZAPI.hostinterface.get(
                        **{
                            'output': 'extend',
                            'hostids': hostId
                        }
                    )
                except Exception as e:
                    # 現状のホストのインターフェイス情報取得失敗
                    # 表示（仮）
                    print('X', end='', flush=True)
                    disp += 'Failed get Interfaces on Node, %s.' % e
                    display.append(disp)
                    dispCount += 1
                    continue

                # インターフェイスの確認
                types = [item['type'] for item in hostIfs]
                if len(hostIfs) != len(list(set(types))):
                    # 同じ種類が複数ある（ので重複排除で少なくなる）
                    if len(hostIfs) == 2 and len(list(set(types))) == 1:
                        # インターフェイスが２つ、どちらも同じtypeなのでアップデート可
                        pass
                    else:
                        # 対応できないインターフェイス設定なのでアップデート不可、スキップ
                        # 表示（仮）
                        print('X', end='', flush=True)
                        disp += 'Failed, Type >= 2 with Interface >= 2 on Node.'
                        display.append(disp)
                        dispCount += 1
                        continue
                else:
                    # 全部違う種類（typeで判断できる）で１つずつだけなのでアップデート可
                    pass
                for updateIf in updateIfs['data']:
                    # typeとmainが同じインターフェイスを選択
                    targetIf = [
                        item for item in hostIfs if int(item['type']) == updateIf['type'] and int(item['main']) == updateIf['main']
                    ]
                    if not targetIf or len(targetIf) > 1:
                        # このパターンはないはずだけど一応
                        continue
                    targetIf = targetIf[0]
                    # アップデートするインターフェイスはリストから消す、残ったインターフェイスは削除対象
                    hostIfs.remove(targetIf)
                    # ターゲットのdetailsが空の時は[]になっているというクソ仕様（中身があるとdict、型が違う）
                    # 空の時はdetailsを消す
                    if not targetIf.get('details'):
                        targetIf.pop('details')

                    # 表示（仮）
                    disp += ZABBIX_IFTYPE[updateIf['type']]
                    if updateIf['main'] == 1:
                        disp += '(default)'

                    # 変更箇所の確認
                    change = False
                    for param, value in updateIf.items():
                        # 変更が一つでもあれば更新
                        if param == 'details':
                            for detail, dVal in updateIf['details'].items():
                                if targetIf['details'].get(detail) != str(dVal):
                                    change = True
                                    break
                        else:
                            if targetIf.get(param) != str(value):
                                change = True
                                break
                    if not change:
                        # 表示（仮）
                        print('.', end='')
                        dispCount += 1
                        # 変更がないのでスキップ
                        continue
                    updateIf['interfaceid'] = targetIf['interfaceid']
                    try:
                        result = self.ZAPI.hostinterface.update(**updateIf)
                        # 表示（仮）
                        print('U', end='')
                    except Exception as e:
                        # 表示（仮）
                        print('X', end='')
                        disp += f'{e}'

                    # 表示（仮）
                    display.append(disp)
                    dispCount += 1
                    if dispCount == WIDE_COUNT:
                        print(f'\n{TAB*3}', end='', flush=True)

                for hostIf in hostIfs:
                    # 削除対象の処理
                    deleteInterfaces.append(
                        {
                            'name': '%s(%s)' % (hostName, ZABBIX_IFTYPE[int(hostIf['type'])]),
                            'id': hostIf['interfaceid']
                        }
                    )
            # 表示（仮）
            print('')
        else:
            pass

        # 表示（仮）
        if display:
            print(f'\n{TAB*3}Interface Update Error:', flush=True)
            for disp in display:
                print(disp)
        
        if deleteInterfaces:
            # 表示（仮）
            print(f'{TAB*2}Delete Interface(s):', end='', flush=True)

            for delIf in deleteInterfaces:
                name = delIf['name']
                try:
                    self.ZAPI.hostinterface.delete(delIf['id'])
                    res = 'Success'
                except Exception as e:
                    res = f'Failed, {e}'

                # 表示（仮）
                print(f'{TAB*3}{name}: {res}')

        # Zabbixからのデータ再取得
        self.getDataFromZabbix()            

        # ホスト削除
        # ストアデータに存在しないホストは削除する
        # 対象IDリスト
        deleteTarget = []
        result = []
        # update/createの両方処理済みのホスト、ここにないものを削除する
        importHosts = [host['name'] for host in hosts]
        for name, item in self.LOCAL['host'].items():
            if name not in importHosts:
                deleteTarget.append(item['ZABBIX_ID'])
        if deleteTarget and not self.CONFIG.noDelete:
            try:
                self.ZAPI.host.delete(*deleteTarget)
                # ID->名前変換
                deleteTarget = '/'.join([self.replaceIdName('host', host) for host in deleteTarget])
                result = True
            except Exception as e:
                result = e
            # Zabbixからのデータ再取得
            self.getDataFromZabbix()

            # 表示（仮）
            print(f'{TAB*2}Delete Hosts: {deleteTarget}', flush=True)

            if result is True:
                print('Success.')
            else:
                print(f'Failed, {e}')

        # ホストインポート処理のログ出しまたは画面出力（予）

        return ZC_COMPLETE

    def setVersionCode(self, init=False):
        '''
        グローバルマクロに適用したバージョンの情報を追加する
        init: 初期化フラグがTrueならUUIDではない初期文字列を入れる
        '''
        if self.checkMasterNode():
            version = self.NEW['VERSION_ID']
        else:
            version = '__NOT_YET_CLONE__' if init else self.getLatestVersion('VERSION_ID')

        # ローカルにバージョンのグローバルがあるか確認
        idName = self.getKeynameInMethod('usermacro', 'id')
        versionCode = self.LOCAL['usermacro'].get(ZC_VERSION_CODE)
        if versionCode:
            # あったら更新
            function = 'updateglobal'
            data = {
                idName: versionCode['ZABBIX_ID'],
                'value': version,
            }
        else:
            # なければ追加
            function = 'createglobal'
            data = {
                'macro': ZC_VERSION_CODE,
                'value': version,
            }

        if self.CONFIG.storeType == 'direct':
            data['description'] = 'Master-Node: %s (%s)' % (
                self.CONFIG.storeConnect['direct_node'],
                self.CONFIG.storeConnect['direct_endpoint']
            )

        try:
            getattr(self.ZAPI.usermacro, function)(**data)
        except Exception as e:
            return (False, 'Failed, %s Version:%s. %s' % (function, version, e))

        return ZC_COMPLETE

    def createNewData(self):

        '''
        マスターノードから現在のデータを取得して新バージョンを作る
        ・テンプレートからデータ取得、execConfigurationExport
        ・ZABBIX_ID指定されているAction/Script/MaintenanceのIDをNAMEに変更
        '''

        # バージョンデータを作っていいのはマスターノードだけ
        if not self.checkMasterNode():
            return (False, 'Not Master Node.')

        # ストアデータ格納変数の初期化
        self.STORE = {}

        # 表示（仮）
        print(f'\n{TAB*2}Export Zabbix Configuration: ', end = '', flush=True)

        # configuration.export対象のデータを取得
        result = self.getConfigurationFromZabbix()
        if not result[0]:
            return result

        # 表示（仮）
        print(f'Done.', end='', flush=True)
        print(f'\n{TAB*2}Convert Zabbix Data to Clone Data: ', end='', flush=True)

        # LOCALのデータをSTOREに複製
        for method, data in self.LOCAL.items():
            items = []
            for item in data.values():
                # 特権管理者は処理してはいけないので捨てる
                if method == 'user' and item['NAME'] == ZABBIX_SUPER_USER:
                    continue
                # ユーザーグループの管理者グループは処理してはいけないので捨てる
                if method == 'usergroup' and item['NAME'] == ZABBIX_SUPER_GROUP:
                    continue
                # 5.2対応
                # ロールの特権管理者権限は処理してはいけないので捨てる
                if method == 'role' and item['ZABBIX_ID'] == ZABBIX_SUPER_ROLE:
                    continue
                # マスター側のバージョンコードはここではいらないので抜く
                if method == 'usermacro' and item['NAME'] == ZC_VERSION_CODE:
                    continue
                # ZABBIX_IDはLOCALのものなのでSTOREには不要なので捨てる
                items.append(
                    {
                        'NAME': item['NAME'],
                        'DATA': item['DATA']
                    }
                )
            if items:
                self.STORE[method] = items

        # 表示（仮）
        print(f'Done.', end='', flush=True)
        print(f'\n{TAB*2}Convert PRE section Data: ', end='', flush=True)

        # ID変換が必要なメソッドのデータ変換
        result = self.processingMethodData('PRE')
        if not result[0]:
            return result

        # 表示（仮）
        print(f'Done.', end='', flush=True)
        print(f'\n{TAB*2}Convert MID section Data: ', end='', flush=True)

        result = self.processingMethodData('MID')
        if not result[0]:
            return result

        # 表示（仮）
        print(f'Done.', end='', flush=True)
        print(f'\n{TAB*2}Convert POST section Data: ', end='', flush=True)

        result = self.processingMethodData('POST')
        if not result[0]:
            return result

        # 表示（仮）
        print(f'Done.', end='', flush=True)
        print(f'\n{TAB*2}Convert ACCOUNT section Data: ', end='', flush=True)

        result = self.processingMethodData('ACCOUNT')
        if not result[0]:
            return result

        # 表示（仮）
        print(f'Done.', end='', flush=True)
        print(f'\n{TAB*2}Convert Authentication Data: ', end='', flush=True)

        # GLOBAL内でこれだけデータ変換処理が必要なので実行
        result = self.processingAuthentication()
        if not result[0]:
            return result

        # 表示（仮）
        print(f'Done.', end='', flush=True)

        return ZC_COMPLETE

    def setAlertStopInUpdate(self):
        '''
        アップデート中アラートが発生しないようにメンテナンスを設定する
        メンテナンス期間: 10分
        '''

        # 開始時刻の設定
        now = UNIXTIME()
        # 期間
        period = 600

        # グループID
        gIds = 'groupids'
        # 6.0対応
        if self.VERSION['major'] >= 6.0:
            gIds = 'groups' 

        targets = [item['ZABBIX_ID'] for item in self.LOCAL['hostgroup'].values()]
        # 6.0対応
        if self.VERSION['major'] >= 6.0:
            idName = self.getKeynameInMethod('hostgroup', 'id')
            targets = [{idName: id} for id in targets]

        inUpdate = {
            'name': ZC_MAINTE_NAME,
            'active_since': now,
            'active_till': now + period,
            'maintenance_type': 0,
            'timeperiods' :[
                {
                    'timeperiod_type': 0,
                    'start_date': now,
                    'period': period
                }
            ],
            gIds: targets
        }
        API = getattr(self.ZAPI, 'maintenance')

        # 既存のアップデート中アラート停止の有無を確認、あれば削除
        exists = [item['ZABBIX_ID'] for item in self.LOCAL['maintenance'].values() if item['NAME'] == ZC_MAINTE_NAME]
        if exists:
            try:
                API.delete(*exists)
            except:
                return (False, 'Failed Delete Exist AlertStop.')
        try:
            result = API.create(**inUpdate)
            if not result.get('maintenanceids'):
                return (False, 'Failed Set AlertStop.')
        except Exception as e:
            return (False, 'Failed Set AlertStop. %s' % e)
        
        # Zabbixからのデータ再取得
        self.getDataFromZabbix()

        print(f'\n{TAB*2}AlertStop Period: from NOW to {period}s after.', end='', flush=True)

        return ZC_COMPLETE

    def setAlertMedia(self):
        '''
        アラート情報をユーザーに設定する
        '''
        # 通知設定しないノードは終了
        if self.CONFIG.role in ZC_NO_NOTICE_ROLE:
            return ZC_COMPLETE
        # 有効なメディアタイプがなければ終了
        if not self.LOCAL.get('mediatype'):
            return ZC_COMPLETE
        # ZCに渡されたメディア設定
        mediaSettings = self.CONFIG.mediaSettings
        # {メディア：対象ユーザーデータ}になっているのを{ユーザー：[メディア]}に変換
        # 6.2対応
        if self.VERSION['major'] >= 6.2:
            userMedias = 'medias'
        else:
            userMedias = 'user_medias'
        userMediasData = {}
        for media, values in mediaSettings.items():
            # ZABBIX_ID取得
            id = self.replaceIdName('mediatype', media)
            # なければスキップ
            if not id:
                continue
            for user, value in values.items():
                user = self.replaceIdName('user', user)
                if not user:
                    # ワーカーノードにユーザーがいないものは排除
                    continue
                idName = self.getKeynameInMethod('user', 'id')
                if not value.get('to'):
                    # 宛先設定がないものは排除
                    continue
                if not value.get('severity'):
                    # 対応重要度レベルがないものは排除
                    continue
                if not value.get('work_time'):
                    # 通知可能時間がないものは排除
                    continue
                # 宛先はリストかタプル
                if isinstance(value['to'], (list, tuple)):
                    address = value['to']
                elif isinstance(value['to'], str):
                    address = [value['to']]
                else:
                    continue
                # severity生成
                # 重要度レベルが「543210」（0はUnknown）の並びのビット立てを数字にしたもの
                severity = 0
                for lv in range(0, 6):
                    if value['severity'].get(str(lv), 'NO') == 'YES':
                        severity += 2 ** lv
                # period生成
                # 曜日ごとに「isoweekday,HH:MM-HH:MM」の記述で「;」区切り
                # 曜日は1-7って範囲で書けるけど、ワークタイムが同じ場合のみ範囲で書けるので
                # そこの判定はめんどうなので曜日ごとにする
                period = []
                for wd, time in value['work_time'].items():
                    if not time:
                        continue
                    if not re.match('[0-9].\:[0-9].\-[0-9].\:[0-9].', time):
                        continue
                    period.append('{0},{1}'.format(ZABBIX_WEEKDAY[wd.upper()], time))
                media = {
                    'mediatypeid': id,
                    'sendto': address,
                    'active':0,
                    'severity': severity,
                    'period': ';'.join(period)
                }
                if not userMediasData.get(user):
                    # userMediasDataにuserがない場合は作成
                    userMediasData[user] = {
                        idName: user,
                        userMedias: [media]
                    }
                else:
                    # ある場合はmediaだけ追加
                    userMediasData[user][userMedias].append(media)
        # 適用
        for user, data in userMediasData.items():
            try:
                self.ZAPI.user.update(**data)
            except:
                return (False, 'Failed Set AlertMedia for %s.' % self.replaceIdName('user', user))
        return ZC_COMPLETE

    def setAuthenticationToZabbix(self):
        '''
        Zabbixの認証設定を変更する
        MFAでID変換の必要が入ってきたので独立して最後に実行
        '''
        if not self.STORE.get('authentication'):
            # 認証のAPIがないバージョンではデータがないのでスキップ
            return (True, 'No Exsit Authentication.')
        
        # 認証設定
        data = {}
        [data.update(item['DATA']) for item in self.STORE['authentication']]
        # 6.2以下対応
        # ディレクトリサービス認証を使用しない場合は削除
        if self.VERSION['major'] <= 6.2:
            if not int(data.get('idap_configured', 0)):
                for param in self.discardParameter['authentication']['ldap']:
                    data.pop(param, None)
                data.pop('idap_configured', None)
            if not int(data.get('saml_auth_enabled')):
                for param in self.discardParameter['authentication']['saml']:
                    data.pop(param, None)
                data.pop('saml_auth_enabled', None)
        # 6.2対応
        if self.VERSION['major'] >= 6.2:
            if self.VERSION['major'] == 6.2:
                # バグってLDAPサーバーが設定されていないと０でも１でも弾くので削除、バーカバーカ
                data.pop('authentication_type', None)
            if self.getLatestVersion('MASTER_VERSION') < 6.2:
                # 6.2以降、userdirectoryが新設されてLDAP設定がそちらに移動
                if int(data.get('ldap_configured')):
                    ldapParams = {
                        'name': 'LDAP Converted 6.0 -> 6.2 later by ZC'
                    }
                    for param in self.discardParameter['authentication']['ldap']:
                        value = data.pop(param, '').removeprefix('ldap_')
                        if value:
                            ldapParams.update(
                                {
                                    param.removeprefix('ldap_'): value
                                }
                            )
                    if ldapParams.get('host'):
                        try:
                            res = self.ZAPI.userdirectory.create(**ldapParams)
                        except:
                            res = []
                        if res:
                            data['ldap_auth_enabled'] = 1
                            data['ldap_userdirectoryid'] = res['userdirectoryids'][0]
    
        # 6.4対応
        if self.VERSION['major'] >= 6.4:
            # 古いバージョンのパラメーターだったら変換
            value = data.pop('ldap_configured', None)
            if value:
                data.update({'ldap_auth_enabled': int(value)})
            if self.getLatestVersion('MASTER_VERSION') < 6.4:
                # SAMLがuserdirectoryに移動
                if int(data.get('saml_auth_enabled', 0)):
                    samlParams = {
                        'name': 'SAML Converted 6.0/6.2 -> 6.4 later by ZC',
                        'idp_type': 1
                    }
                    for param in self.discardParameter['authentication']['saml']:
                        value = data.pop(param, '').removeprefix('saml_')
                        if value:
                            samlParams.update(
                                {
                                    param.removeprefix('saml_'): value
                                }
                            )
                    if samlParams.get('idp_entityid'):
                        try:
                            res = self.ZAPI.userdirectory.create(**samlParams)
                        except:
                            res = []
                        if not res:
                            data['saml_auth_enabled'] = 0
            
            # LDAP利用しない
            if int(data.get('ldap_auth_enabled', 0)) == 0:
                ldap = False
                for param in self.discardParameter['authentication']['ldap']:
                    data.pop(param, None)
                data.pop('ldap_auth_enabled', None)
            else:
                ldap = True
            # SAML利用しない
            if int(data.get('saml_auth_enabled', 0)) == 0:
                saml = False
                for param in self.discardParameter['authentication']['saml']:
                    data.pop(param, None)
                data.pop('saml_auth_enabled', None)
            else:
                saml = True
            if ldap or saml:
                # LDAP/SAMLどちらかを利用する場合は変換する
                id = self.replaceIdName('usergroup', data['disabled_usrgrpid'])
                if id:
                    data['disabled_usrgrpid'] = id
            else:
                # LDAP/SAMLどちらも利用しない場合
                data.pop('disabled_usrgrpid', None)
        # 7.0対応
        if self.VERSION['major'] >= 7.0:
            # MFA利用
            if int(data.get('mfa_status', 0)) == 0:
                data.pop('mfa_status', None)
                data.pop('mfaid', None)
            else:
                # デフォルト利用のMFAのID変換処理
                id = self.replaceIdName('mfa', data['mfaid'])
                if id:
                    data['mfaid'] = id
        # ZabbixCloud対応: HTTP AUTH関連が存在しない
        if self.CONFIG.zabbixCloud:
            for property in self.zabbixCloudSpecialItem['authentication']:
                data.pop(property, None)
        try:
            self.ZAPI.authentication.update(**data)
        except Exception as e:
            return (False, 'Failed, set Authentication. %s' % e)

        return ZC_COMPLETE

    def execCheckNow(self):
        '''
        LLDとLONGTIMEインターバルアイテムを初回実行する
        '''
        # CheckNowを実行するか確認
        if not self.CONFIG.checknowExec:
            return (True, 'SKIP.')

        def checknow(targets):
            '''
            ファンクション内CheckNow実行ファンクション
            '''
            # 5.2対応
            if self.VERSION['major'] >= 5.2:
                option = []
                for target in targets:
                    option.append(
                        {
                            'type': '6',
                            'request': {'itemid': target}
                        }
                    )
            else:
                option = {
                    'type': '6',
                    'itemids': targets
                }
            try:
                # DB上のデータがZabbixサーバーに適用されるのを待つ
                sleep(self.CONFIG.checknowWait)
                self.ZAPI.task.create(*option)
                return 'OK'
            except Exception as e:
                return f'NG, {e}'
        
        # 更新間隔サーチワード
        interval = []
        for item in self.CONFIG.checknowInterval:
            time = item[:-1]
            suffix = item[-1]
            if time.isdigit() and not suffix.isdigit():
                time = int(time)
                if suffix == 'm':
                    time *= 60
                elif suffix == 'h':
                    time *= 3600
                elif suffix == 'd':
                    time *= 86400
                else:
                    try:
                        time = int(item)
                    except:
                        continue
            interval.append(str(time))
        interval = set(sorted(interval))

        # Zabbixに適用されているホスト
        hosts = [item['ZABBIX_ID'] for item in self.LOCAL['host'].values()]

        # LLDを検索
        output = ['itemid']
        # 4.2対応
        if self.VERSION['major'] >= 4.2:
            output.append('master_itemid')
        try:
            targets = self.ZAPI.discoveryrule.get(
                output=output,
                hostids=hosts
            )
            # 依存元アイテムのはmaster_itemidを、それ以外はitemidを使う
            targets = [
                item['master_itemid'] if int(item['master_itemid']) else item['itemid'] for item in targets
            ]
        except:
            targets = []

        # 表示（仮）
        print(f'\n{TAB*2}LLDs {len(targets)} items (wait {self.CONFIG.checknowWait}s): ', end='', flush=True)
        if not targets:
            print('No Exist LLDs items.')
        else:
            # LLDへのCheckNow実行
            res = checknow(targets)

        # 表示（仮）
        print(f'{res}', end='', flush=True)

        # 更新間隔にユーザーマクロで部分一致文字列を適用しているものを抽出
        try:
            targets = self.ZAPI.item.get(
                output=output,
                hostids=hosts,
                filter={'delay': interval}
            )
            # 依存元アイテムのはmaster_itemidを、それ以外はitemidを使う
            targets = [
                item['master_itemid'] if int(item['master_itemid']) else item['itemid'] for item in targets
            ]
        except:
            targets = []
        if targets:
            # 表示（仮）
            interval = '/'.join(interval)
            print(f'\n{TAB*2}TargetInterval[{interval}] {len(targets)} items (wait {self.CONFIG.checknowWait}s): ', end='', flush=True)

            # 実行
            res = checknow(targets)

            # 表示（仮）
            print(f'{res}', end='', flush=True)


        return ZC_COMPLETE

def inputParameters():
    '''
    パラメーター入力処理
    優先順位：
    ↑コマンド引数
      環境変数
    ↓設定ファイル
    
    '''
    params = {'store_connect': {}, 'db_connect': {}}
    # 環境変数読み込み
    for env, value in os.environ.items():
        env = env.upper()
        if not re.match(ZC_HEAD, env):
            continue
        env = env.removeprefix(ZC_HEAD).lower()
        if re.match('^[a-z]*_connect_', env):
            env = env.split('_')
            params['_'.join(env[:2])].update(
                {
                    '_'.join(env[2:]): value
                }
            )
        else:
            params.update({env: value})

    parser = argparse.ArgumentParser(
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=textwrap.dedent('''\
        Zabbix Clone: Zabbix monitoring settings cloning tool, from master-Zabbix to worker-Zabbix.
        If you use datastore, can manage settings by versions.''')
    )
    parser.add_argument(
        'command',
        choices=['clone', 'showversions', 'showdata'],
        help='clone: Execute Cloning, showversions: show versions in store, showdata: show version\'s data(requierd ---version)'
    )
    parser.add_argument(
        '-v', '--version',
        help='version指定'
    )
    '''parser.add_argument(
        '-q', '--quiet',
        action='store_true',
        help='処理進捗を表示しない'
    )'''
    parser.add_argument(
        '--method',
        nargs='+',
        help='表示機能で指定のメソッドのみ表示する'
    )
    parser.add_argument(
        '--name',
        nargs='+',
        help='表示機能で指定の名前のみ表示する'
    )
    parser.add_argument(
        '--id-only',
        action='store_true',
        help='表示機能で簡略表示をする'
    )
    configGroup = parser.add_argument_group('Configureation File Options')
    configGroup.add_argument(
        '-f', '--config-file',
        help='設定ファイルの指定'
    )
    configGroup.add_argument(
        '--no-config-files',
        action='store_const',
        const='YES',
        help='設定ファイルを利用しない'
    )
    baseGroup = parser.add_argument_group('Base Settings')
    baseGroup.add_argument(
        # ノード名
        '-n', '--node',
        help='ノードの名称'
    )
    baseGroup.add_argument(
        # ロール
        '-r', '--role',
        choices=['master', 'worker', 'replica'],
        help='ノードの役割（デフォルト: worker）'
    )
    connectionGroup = parser.add_argument_group('Base Connection Settings')
    connectionGroup.add_argument(
        # Zabbixエンドポイント
        '-e', '--endpoint',
        help='ノードのZabbixエンドポイント'
    )
    connectionGroup.add_argument(
        '-u', '--user',
        help='複製実行ユーザー'
    )
    connectionGroup.add_argument(
        '-p', '--password',
        help='複製実行ユーザーのパスワード'
    )
    connectionGroup.add_argument(
        '-t', '--token',
        help='複製実行ユーザーのトークン'
    )
    connectionGroup.add_argument(
        '--http-auth',
        action='store_const',
        const='YES',
        help='HTTP AUTHを利用する'
    )
    connectionGroup.add_argument(
        '--self-cert',
        action='store_const',
        const='YES',
        help='自己証明書を確認しない'
    )
    processingGroup = parser.add_argument_group('Processing Options')
    processingGroup.add_argument(
        '--update-password',
        action='store_const',
        const='YES',
        help='複製実行ユーザーのパスワードを--passwordの指定に変更する'
    )
    processingGroup.add_argument(
        '--force-initialize',
        action='store_const',
        const='YES',
        help='ワーカーノードを強制初期化する'
    )
    processingGroup.add_argument(
        '--force-useip',
        action='store_const',
        const='YES',
        help='ホストのエンドポイントをIP利用に強制する'
    )
    processingGroup.add_argument(
        '--force-host-update',
        action='store_const',
        const='YES',
        help='ホストが別の設定で存在していても設定を上書きする'
    )
    processingGroup.add_argument(
        '--no-delete',
        action='store_const',
        const='YES',
        help='マスターノードの設定に存在しない監視設定を削除しない'
    )
    processingGroup.add_argument(
        '--template-skip',
        action='store_const',
        const='YES',
        help='テンプレートのインポート/エクスポートをスキップする'
    )
    processingGroup.add_argument(
        '--template-separate',
        type=int,
        help='テンプレートのエクスポートを区切って処理する数（デフォルト: 100）'
    )
    processingGroup.add_argument(
        '--checknow-execute',
        action='store_const',
        const='YES',
        help='ホスト追加後にLLDや指定監視間隔のアイテムの値取得を実行する'
    )
    processingGroup.add_argument(
        '--checknow-interval',
        nargs='+',
        help='アイテムの値取得を実行する対象の監視間隔'
    )
    processingGroup.add_argument(
        '--php-worker-num',
        type=int,
        help='ホスト追加の並列実行を行う数（デフォルト: 4）'
    )
    storeGroup = parser.add_argument_group('Store Settings')
    storeGroup.add_argument(
        '-s', '--store-type',
        choices=['file', 'redis', 'dydb', 'direct'],
        help='データストアの指定'
    )
    storeGroup.add_argument(
        '-se', '--store-endpoint',
        help='ストアのエンドポイント指定、dydb(aws region), redis(IP/FQDN), direct(URL)'
    )
    storeGroup.add_argument(
        '-sp', '--store-port',
        help='ストアのポート指定、redis(default: 6379)'
    )
    storeGroup.add_argument(
        '-sa', '--store-access',
        help='ストアのアクセス情報、dydb(aws access id), direct(マスターノード名)'
    )
    storeGroup.add_argument(
        '-sc', '--store-credential',
        help='ストアの認証情報、dydb(aws secret key),redis(password), direct(マスターノードトークン)'
    )
    storeGroup.add_argument(
        '-sl', '--store-limit',
        type=int,
        help='ストアの処理分離数、dydb(default: 10)'
    )
    storeGroup.add_argument(
        '-sw', '--store-interval',
        type=int,
        help='ストアの処理分離時のインターバル秒数、dydb(default: 2)'
    )
    '''
    storeGroup.add_argument(
        '--extend-store',
        help='データストアの指定でextendを選択したときの対象'
    )
    storeGroup.add_argument(
        '--extend-params',
        help='エクステンドストアのパラメーター、JSONのみ'
    )
    '''
    databaseGroup = parser.add_argument_group('Database Connection Settings')
    databaseGroup.add_argument(
        '-dbhost', '--db-connect-host',
        help='Zabbix DBエンドポイント（Zabbix6.0未満対応）'
    )
    databaseGroup.add_argument(
        '-dbname', '--db-connect-name',
        help='Zabbix DB名（Zabbix6.0未満対応）'
    )
    databaseGroup.add_argument(
        '-dbtype', '--db-connect-type',
        choices=['pgsql', 'mysql'],
        help='Zabbix DB種別（Zabbix6.0未満対応）'
    )
    databaseGroup.add_argument(
        '-dbuser', '--db-connect-user',
        help='Zabbix DB接続ユーザー（Zabbix6.0未満対応）'
    )
    databaseGroup.add_argument(
        '-dbpswd', '--db-connect-password',
        help='Zabbix DB接続パスワード（Zabbix6.0未満対応）'
    )
    parser = parser.parse_args()
    params = {}
    for parse, value in parser.__dict__.items():
        if not value:
            continue
        if re.match('^[a-z]*_connect_', parse):
            parse = parse.split('_')
            connect = '_'.join(parse[:2])
            if not params.get(connect):
                params[connect] = {}
            params[connect].update({parse[-1]: value})
        else:
            params.update({parse: value})
    return params

def main():
    params = inputParameters()
    if not params:
        sys.exit('wrong parameters')
    # 実行コマンド
    command = params.pop('command', 'clone')
    # 進捗の表示
    if command != 'clone':
        quiet = True
    else:
        quiet = params.pop('quiet', False)
    # clone以外の動作: ターゲットのものだけ表示する
    targetMethod = params.pop('method', None)
    targetName = params.pop('name', None)
    idOnly = params.pop('id_only', None)
    # コンフィグの読み込み
    config = ZabbixCloneConfig(**params)
    if command == 'clone':
        node = ZabbixClone(config)

        if config.storeType == 'direct':
            masterConfig = ZabbixCloneConfig(**params)
            masterConfig.changeDirectMaster()
            master = ZabbixClone(masterConfig)

        # 表示（仮）
        if not quiet:
            config.showParameters()
            print(f'\n[START] {ZABBIX_TIME()}')

        # 実行処理リスト
        functions = [
            ['firstProcess', None]
        ]

        if node.checkMasterNode():
            # マスターノード処理
            # 新バージョンデータの生成
            # データストアへのアップロード
            functions += [
                ['createNewData',         None],
                ['setVersionDataToStore', None]
            ]
        else:
            # ワーカーノード処理
            # グローバル設定の適用
            # nodeインスタンスのデータへ最新バージョンを適用
            # APIセクションの適用（usermacro/usergroup/user/...）
            # CONFIG_IMPORTセクションのインポート生成&実行（hostgroup/templategroup/template/mediatype/trigger）
            # host適用（ここでのメイン記述）
            # Zabbixからのデータ再取得
            # REPLACEセクションの適用（action/script/maintenance/...）
            # ACCOUNTセクションの適用（user/usergroup/role）
            # AFTERセクションの適用（service / serviceExtend）
            # 初期イベント抑止のためのメンテナンス適用
            # アラート実行ユーザーへのメディア設定適用
            # 初回LLDの実行の対象指定、するかどうか

            # パスワード変更
            functions += [
                ['changePassword', None]
            ]

            # Directモードの時はデータを直接マスターノードから読み込む
            if config.storeType == 'direct':
                functions += ['getDataFromStore', {'master': master}],
            else:
                functions += ['getDataFromStore', None],
            
            functions += [
                ['setGlobalsettingsToZabbix', None],
                ['setApiToZabbix',            {'section': 'PRE'}],
                ['setConfigurationToZabbix',  None],
                ['setAlertStopInUpdate',      None],
                ['setApiToZabbix',            {'section': 'MID'}],
                ['setHostToZabbix',           None],
                ['execCheckNow',              None],
                ['setApiToZabbix',            {'section': 'POST'}],
                ['setApiToZabbix',            {'section': 'ACCOUNT'}],
                ['setApiToZabbix',            {'section': 'EXTEND'}],
                ['setAuthenticationToZabbix', None],
                ['setAlertMedia',             None],
            ]

        functions += [
            # 現在適用バージョンを記録
            ['setVersionCode', None],
        ]

        for function in functions:
            func = function[0]
            option = function[1]
            if not quiet:
                execute = f'{config.role}({config.node}).{func}'
                print(f'{TAB}{execute}', end=':', flush=True)
            try:
                if option:
                    result = getattr(node, func)(**option)
                else:
                    result = getattr(node, func)()
            except Exception as e:
                print(f'{e}')
                sys.exit('Execute Function Failed: [%s] ' % '/'.join(function[:1]))
            if not quiet:
                if isinstance(result[1], (dict, list, tuple)):
                    output = json.dumps(result[1], indent=TAB) + f'\n{ZC_COMPLETE[1]}'
                else:
                    output = result[1]
            if not result[0]:
                print(f'\n\n[ABORT] {ZABBIX_TIME()}')
                print(f'{TAB*2}' + output.replace('\n', f'\n{TAB*2}'))
                sys.exit(255)
            print(f'\n{TAB*2}' + output.replace('\n', f'\n{TAB*2}') + '\n')
        print(f'\n[END] {ZABBIX_TIME()}')
    else:
        # これ以下の実装、全部仮
        # clone以外の動作
        # ノードを初期化
        # VERSIONの取得
        print(f'STORE TYPE:[ {config.storeType} ] / COMMAND: {command}')
        if config.storeType == 'direct':
            config.changeDirectMaster()
            node = ZabbixClone(config)
        else:
            node = ZabbixCloneDatastore(config)
            result = node.getVersionFromStore()
            if not result[0]:
                sys.exit(result[1])
        if command in ['showdata', 'delete']:
            # DATA取得実行
            if config.directMaster:
                result = node.getDataFromMaster()
                store = node.STORE
                if not result[0]:
                    sys.exit(result[1])
            else:
                if not params.get('version'):
                    sys.exit(f'{command} Required --version.')
                result = node.getVersionFromStore(params['version'])
                if not result[0]:
                    sys.exit(result[1])
                target = result[1][0]
                result = node.getDataFromStore(target)
                if not result[0]:
                    sys.exit(result[1])
                if isinstance(result[1], list):
                    store = {}
                    for item in result[1]:
                        method = item['METHOD']
                        if not store.get(method):
                            store[method] = []
                        store[method].append(item)
                else:
                    store = node.STORE
        if command == 'showversions':
            if config.directMaster:
                print('DirectMode Connot Execute showversions.')
                sys.exit(0)
            title = 'In Store Versions:'
            print(f'{title}{B_CHAR*(WIDE_COUNT-len(title))}')
            for ver in node.VERSIONS:
                if idOnly:
                    vId = ver['VERSION_ID']
                    unixtime = ver['UNIXTIME']
                    print(f'{TAB}{vId}: {unixtime}')
                else:
                    output = json.dumps(ver, indent=TAB)
                    print(f'{TAB}' + output.replace('\n', f'\n{TAB}'))
                    print(f'{TAB}{BD}')
        elif command == 'showdata':
            for method, items in store.items():
                if not targetMethod or method in targetMethod:
                    print(f'{method}:{B_CHAR*(WIDE_COUNT-len(method)-1)}')
                    items = sorted(items, key=lambda x:x['NAME'])
                    for item in items:
                        if not targetName or item['NAME'] in targetName:
                            if idOnly:
                                name = item['NAME']
                                dId = item.get('DATA_ID', 'DIRECT-MODE')
                                print(f'{TAB}{dId}: {name}')
                            else:
                                output = json.dumps(item, indent=TAB)
                                print(f'{TAB}' + output.replace('\n', f'\n{TAB}'))
                                print(f'{TAB}{BD}')
        elif command == 'delete':
            print('未実装')
        elif command == 'clearstore':
            node.clearStore()
        else:
            pass
            
    sys.exit(0)

if __name__ == '__main__':
    main()

#EOS