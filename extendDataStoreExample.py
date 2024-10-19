EXTEND_COMPLETE = (True, 'COMPLETE')

def initStoreSetting(**params):
    '''
    ストアの接続設定初期化
    返値: (boolean, storeTables)
    '''
    if not params.get('storeConnect'):
        result = (False, 'error message.')
    storeTables = {}
    result = (True, storeTables)
    # 処理
    return result

def clearStore(**params):
    '''
    ストア上の対象テーブル[VERSION, DATA]の内容すべて削除
    返値: (boolean, 'message')
    '''
    tables = params.get('tables')
    if not tables:
        result = (False, 'error message.')
    result = EXTEND_COMPLETE
    # 処理
    for table in tables:
        pass
    return result

def deleteRecordInStore(**params):
    '''
    対象バージョンのレコードをDATAテーブルから消す
    返値: (boolean, 'message')
    '''
    version = params.get('version')
    if not version:
        result = (False, 'error message.')
    data = params.get('data')
    if not data:
        result = (False, 'error message.')
    result = EXTEND_COMPLETE
    # 処理
    return result

def deleteVersionInStore(**params):
    '''
    対象バージョンを消す
    返値: (boolean, 'message')
    '''
    version = params.get('version')
    if not version:
        result = (False, 'error message.')
    result = EXTEND_COMPLETE
    # 処理
    return result

def getVersionFromStore(**params):
    '''
    VERSIONの全データを取得
    返値: (boolean, versions)
    '''
    version = params.get('version')
    client = params.get('client')
    if not client:
        return (False, 'No Exist VERSION client.')
    versions = []
    try:
        if version:
            # 特定バージョンのみを取得またはフィルタリング
            pass
        result = (True, versions)
    except Exception as e:
        result = (False, str(e))
    return result

def setVersionToStore(**params):
    '''
    VERSIONデータをストアに追加する
    返値: (boolean, message)
    '''
    version = params.get('version')
    if not version:
        return (False, 'No Exsit VERSION data.')
    client = params.get('client')
    if not client:
        return (False, 'No Exist VERSION client.')
    versions = []
    try:
        # 処理
        pass
        result = (True, versions)
    except Exception as e:
        result = (False, str(e))
    return result

def getDataFromStore(**params):
    '''
    ストアから対象バージョンのDATAを取得する
    返値: (boolean, [{item},...])
    '''
    result = []
    version = params['version']
    client = params['client']
    try:
        items = client.getProcessing(version)
        pass
    except Exception as e:
        return (False, result)
    # データ成型
    for item in items:
        try:
            # item = {METHOD:'', 'DATA_ID': '', 'NAME':'', 'DATA': {ZabbixMethodData}}
            result.append(f'anyProcessing to {item}')
        except Exception as e:
            print(e)
            return (False, result)
    return (True, result)

def setDataToStoreRedis(**params):
    '''
    Redisにデータを追加する
    返値: (boolean, message)
    '''
    result = EXTEND_COMPLETE
    # dataset = {'METHOD': [{'DATA_ID': '', 'NAME': '', 'DATA': {ZabbixMethodData}}], {},...}
    version = params['version']
    dataset = params['dataset']
    client = params['client']
    try:
        for method, items in dataset.items():
            for item in items:
                data = {
                    'DATA_ID': item['DATA_ID'],
                    'METHOD': method,
                    'NAME': item['NAME'],
                    'DATA': item['DATA']
                }
                client.setProcessing(version['VERSION_ID'], data)
    except Exception as e:
        result = (False, 'Failed pipeline, %s' % e)
    return result
