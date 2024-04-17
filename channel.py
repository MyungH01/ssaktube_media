from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel
import boto3
from botocore.exceptions import ClientError
from boto3.dynamodb.conditions import Key
import random
import pymysql
from databases import Database
from dotenv import load_dotenv
import os

app = FastAPI()

class ChannelCreate(BaseModel):
    userid: str
    id_token: str

load_dotenv()

def aws_credentials(id_token):
    
    # Cognito 인증 정보 획득
    region = os.getenv('REGION')
    user_pool_id = os.getenv('USER_POOL_ID')
    identity_pool_id = os.getenv('IDENTITY_POOL_ID')

    cognito = boto3.client('cognito-identity', region_name=region)
    response = cognito.get_id(
        IdentityPoolId=identity_pool_id,
        Logins={
            f'cognito-idp.{region}.amazonaws.com/{user_pool_id}': id_token
        }
    )
    identity_id = response['IdentityId']

    credentials_response = cognito.get_credentials_for_identity(
        IdentityId=identity_id,
        Logins={
            f'cognito-idp.{region}.amazonaws.com/{user_pool_id}': id_token
        }
    )
    credentials = credentials_response['Credentials']

    ivs_cog_client = boto3.client(
        'ivs',
        region_name=region,
        aws_access_key_id=credentials['AccessKeyId'],
        aws_secret_access_key=credentials['SecretKey'],
        aws_session_token=credentials['SessionToken']
    )
    return ivs_cog_client

# 레코딩
recording_configuration_arn = os.getenv('RECORDING_CONFIGURATION_ARN')

# 클라이언트 생성
ivs_client = boto3.client('ivs', region_name='ap-northeast-1')
dynamodb = boto3.resource('dynamodb', region_name='ap-northeast-1') 
table = dynamodb.Table('userData')

database_url = os.getenv('DATABASE_URL')
database = Database(database_url)

# userid와 같은 이름으로 ivs 채널 생성 후 userid에 해당하는 DynamoDB 컬럼 channel_id, stream_key, ingest_endpoint, playback_url 데이터 값 업데이트
@app.put("/api/channel")
def create_channel(body: ChannelCreate):
    
    channel_dict = body.dict()
    channel_name = channel_dict['userid']
    id_token = channel_dict['id_token']

    ivs_cog_client = aws_credentials(id_token)
    # 채널 생성
    try:
        response = table.query(
            KeyConditionExpression=Key('userid').eq(channel_name)
        )
        items = response.get('Items', [])
        for item in items:
            if item['channelid'] != 'no-broadcast':
                return {"error": "Channel already exists"}

        create_response = ivs_cog_client.create_channel(
            name=channel_name,
            type='STANDARD',  # BASIC, STANDARD, ADVANCED_SD, ADVANCED_HD 중 선택
            recordingConfigurationArn= recording_configuration_arn
        )
        # 생성된 채널의 스트림 키 및 기타 정보 추출
        channel_arn = create_response['channel']['arn']
        channel_id_part = channel_arn.split(':')[-1]
        channel_id = channel_id_part.split('/')[-1]
        stream_key = create_response['streamKey']['value']
        ingest_endpoint = f"rtmps://{create_response['channel']['ingestEndpoint']}:443/app/"
        # 생성된 채널 정보 검색
        channel_response = ivs_cog_client.get_channel(arn=channel_arn)
        playback_url = channel_response['channel'].get('playbackUrl')

        # DynamoDB에 채널 정보 업데이트
        update_response = table.update_item(
            Key={
                'userid': channel_name  # DynamoDB 테이블의 파티션 키
            },
            UpdateExpression='SET channelid = :val1, streamkey = :val2, ingestendpoint = :val3, streamurl = :val4, ischannel = :val5, streamname = :val6',
            ExpressionAttributeValues={
                ':val1': channel_id,
                ':val2': stream_key,
                ':val3': ingest_endpoint,
                ':val4': playback_url,
                ':val5': int(1),
                ':val6': "첫방송"
            },
            ReturnValues="UPDATED_NEW"
        )

    except ClientError as e:
        return {"error": str(e)}

    return {
        "channel_arn": channel_id,
        "stream_key": stream_key,
        "ingest_endpoint": ingest_endpoint,
        "playback_url": playback_url 
    }

@app.get("/api/live")
async def get_live_channels():
    response = table.query(
        IndexName='isstream-index',
        KeyConditionExpression=Key('isstream').eq(1),
        ProjectionExpression='thumbnailurl, streamurl, username, channelid, streamname, userlogo, userid'
    )

    base_arn = os.getenv('BASE_ARN')
    live_channels = response.get('Items', [])
    for item in live_channels:
        channel_arn = f"{base_arn}/{item['channelid']}"
        try:
            ivs_response = ivs_client.get_stream(channelArn=channel_arn)
            item['viewerCount'] = ivs_response['stream'].get('viewerCount', 0)
        except:
            item['viewerCount'] = 0

    # 시청자 수에 따라 정렬
    sorted_channels = sorted(live_channels, key=lambda x: x['viewerCount'], reverse=True)
    
    # 대표 방송: 가장 많은 시청자와 가장 적은 시청자
    try:
        if len(sorted_channels) > 1:
            sample_channel = [sorted_channels[0],sorted_channels[-1]]
        else:
            sample_channel = [sorted_channels[0]] 
    except:
        sample_channel = []
    # 메인 라이브: 상위 8개 채널
    main_live = sorted_channels[:8]

    return {
        'sample_channel': sample_channel,
        'main_live': main_live,
    }

@app.get("/api/live/recommend")
async def get_live_channels():
    response = table.query(
        IndexName='isstream-index',
        KeyConditionExpression=Key('isstream').eq(1),
        ProjectionExpression=' username, channelid, streamname, userlogo, userid'
    )

    base_arn = os.getenv('BASE_ARN')
    live_channels = response.get('Items', [])
    for item in live_channels:
        channel_arn = f"{base_arn}/{item['channelid']}"
        try:
            ivs_response = ivs_client.get_stream(channelArn=channel_arn)
            item['viewerCount'] = ivs_response['stream'].get('viewerCount', 0)
        except:
            item['viewerCount'] = 0

    # 시청자 수에 따라 정렬
    sorted_channels = sorted(live_channels, key=lambda x: x['viewerCount'], reverse=True)

    # 추천 채널: 랜덤 5개 채널, 시청자 순 정렬
    sample_size = min(5, len(sorted_channels)) 
    recommend_channel = sorted(random.sample(sorted_channels, sample_size), key=lambda x: x['viewerCount'], reverse=True)

    return {
        'recommend_channel': recommend_channel
    }

@app.get("/api/lives")
async def get_live_channels(page: int = 1, limit: int = 20):
    response = table.query(
        IndexName='isstream-index',
        KeyConditionExpression=Key('isstream').eq(1),
        ProjectionExpression='thumbnailurl, streamurl, username, channelid, streamname, userlogo, userid'
    )

    base_arn = os.getenv('BASE_ARN')
    live_channels = response.get('Items', [])
    for item in live_channels:
        channel_arn = f"{base_arn}/{item['channelid']}"
        try:
            ivs_response = ivs_client.get_stream(channelArn=channel_arn)
            item['viewerCount'] = ivs_response['stream'].get('viewerCount', 0)
        except:
            item['viewerCount'] = 0

    # 시청자 수에 따라 정렬
    sorted_channels = sorted(live_channels, key=lambda x: x['viewerCount'], reverse=True)

    # 페이지네이션
    start = (page - 1) * limit
    end = start + limit
    paged_channels = sorted_channels[start:end]

    return {
         'page_channels': paged_channels
    }

# 채널명 Live Stream API - 라이브페이지 상세
# DynamoDB에서 userid, streamurl, streamname, username, userlogo 값을 가져오고, IVS에서 viewerCount 값을 가져옴
@app.get("/api/live/{channelid}")
async def read_item_by_channelid(channelid: str):
    response = table.query(
        IndexName='channelid-index',
        KeyConditionExpression=Key('channelid').eq(channelid),
        ProjectionExpression=' streamurl, streamname, username, userlogo, isstream, streamstarttime, streamendtime, userid ' 
    )
    items = response.get('Items', [])

    base_arn = os.getenv('BASE_ARN')
    full_channel_arn = f"{base_arn}/{channelid}"

    try:
        ivs_response = ivs_client.get_stream(channelArn=full_channel_arn)
        viewrCount = ivs_response['stream'].get('viewerCount')
    except:
        viewrCount = 0
        
    # 스트림 정보에서 필요한 값 추출 및 합치기

    return  {
        **items[0],
        'viewerCount': viewrCount
    }

@app.post("/api/user/{userid}")
async def read_item_by_userid(userid: str):
    
    response = table.query(
        KeyConditionExpression=Key('userid').eq(userid),
        ProjectionExpression='streamurl, streamname, username, userlogo, isstream, streamstarttime, streamendtime, userid, channelid'
    )
    items = response.get('Items', [])

    if items and 'channelid' in items[0]:
        channelid = items[0]['channelid']
        
        base_arn = os.getenv('BASE_ARN')
        full_channel_arn = f"{base_arn}/{channelid}"

        try:
            ivs_response = ivs_client.get_stream(channelArn=full_channel_arn)
            viewerCount = ivs_response['stream'].get('viewerCount', 0)
        except:
            viewerCount = 0
        
        return  {
            **items[0],
            'viewerCount': viewerCount
        }
    
# 매 요청마다 DB 연결하지않기 위해 설정
@app.on_event("startup")
async def startup():
    await database.connect()

@app.on_event("shutdown")
async def shutdown():
    await database.disconnect()

# 전체 방송인 다시보기 페이지 24시간 동안 인기순으로 - 60개 
@app.get("/api/replays")
async def get_all_replay():
    query = """
    SELECT idx, userid, channelid, replayurl, recordingstart, recordingend, viewercount, streamname, nickname, userlogo
    FROM replayview
    WHERE recordingend >= date_sub(now(), interval 1 day) and  recordingend < now()
    ORDER BY viewercount DESC
    LIMIT 60;
    """
    result = await database.fetch_all(query)
    if not result:
        return []
    return result

# 전체 VOD 페이지 - 40개
# ?sort=popular&page=1 인기 있는순
# ?sort=latest&page=1  최신저장
@app.get("/api/vods")
async def post_replay_info(page: int = Query(1, gt=0), sort: str = Query('latest', enum=['latest', 'popular'])):
    items_per_page = 40
    offset = (page - 1) * items_per_page

    if sort == 'latest':
        order_clause = "ORDER BY idx DESC"
    elif sort == 'popular':
        order_clause = "ORDER BY viewercount DESC"

    # 데이터 개수를 계산하는 쿼리
    count_query = "SELECT COUNT(userid) AS count FROM vodview"
    total_count_result = await database.fetch_one(count_query)
    total_count = total_count_result['count']

    # 페이지 수 계산
    total_pages = (total_count + items_per_page - 1) // items_per_page

    query = f"""
    SELECT replayurl, recordingstart, recordingend, viewercount, userid, idx, streamname, nickname, userlogo, duration
    FROM vodview
    WHERE recordingstart >= date_sub(now(), interval 30 day) and  recordingstart < now() 
    {order_clause}
    LIMIT :limit OFFSET :offset
    """
    result = await database.fetch_all(query, {"limit": items_per_page, "offset": offset})
    if not result:
        return {"total_pages": total_pages, "data": []}
    
    formatted_result = [{
        "replay_url": r['replayurl'],
        "recording_start": r['recordingstart'],
        "recording_end": r['recordingend'],
        "viewer_count": r['viewercount'],
        "userid": r['userid'],
        "idx": r['idx'],
        "streamname": r['streamname'],
        "nickname": r['nickname'],
        "userlogo": r['userlogo'],
        "duration": r['duration']
    } for r in result]

    return {"total_pages": total_pages, "data": formatted_result}

# 개인 다시보기 페이지 - 20개
# ?sort=popular&page=1 인기 있는순
# ?sort=latest&page=1  최신저장
@app.post("/api/user/{userid}/video")
async def post_replay_info(userid: str, page: int = Query(1, gt=0), sort: str = Query('latest', enum=['latest', 'popular'])):
    items_per_page = 20
    offset = (page - 1) * items_per_page

    if sort == 'latest':
        order_clause = "ORDER BY idx DESC"
    elif sort == 'popular':
        order_clause = "ORDER BY viewercount DESC"

    # 데이터 개수를 계산하는 쿼리
    count_query = "SELECT COUNT(userid) AS count FROM godview WHERE userid = :userid"
    total_count_result = await database.fetch_one(count_query, {"userid": userid})
    total_count = total_count_result['count']

    # 페이지 수 계산
    total_pages = (total_count + items_per_page - 1) // items_per_page

    query = f"""
    SELECT replayurl, recordingstart, recordingend, viewercount, userid, idx, streamname, nickname, duration
    FROM godview
    WHERE userid = :userid
    {order_clause}
    LIMIT :limit OFFSET :offset
    """
    result = await database.fetch_all(query, {"userid": userid, "limit": items_per_page, "offset": offset})
    if not result:
        return {"total_pages": total_pages, "data": []}
    
    formatted_result = [{
        "replay_url": r['replayurl'],
        "recording_start": r['recordingstart'],
        "recording_end": r['recordingend'],
        "viewer_count": r['viewercount'],
        "userid": r['userid'],
        "idx": r['idx'],
        "streamname": r['streamname'],
        "nickname": r['nickname'],
        "duration": r['duration']
    } for r in result]

    return {"total_pages": total_pages, "data": formatted_result}

@app.post("/api/user/{userid}/replay")
async def post_replay_info(userid: str, page: int = Query(1, gt=0), sort: str = Query('latest', enum=['latest', 'popular'])):
    items_per_page = 20
    offset = (page - 1) * items_per_page

    if sort == 'latest':
        order_clause = "ORDER BY idx DESC"
    elif sort == 'popular':
        order_clause = "ORDER BY viewercount DESC"

    # 데이터 개수를 계산하는 쿼리
    count_query = "SELECT COUNT(userid) AS count FROM replayview WHERE userid = :userid"
    total_count_result = await database.fetch_one(count_query, {"userid": userid})
    total_count = total_count_result['count']

    # 페이지 수 계산
    total_pages = (total_count + items_per_page - 1) // items_per_page

    query = f"""
    SELECT replayurl, recordingstart, recordingend, viewercount, userid, idx, streamname, nickname, duration
    FROM replayview
    WHERE userid = :userid
    {order_clause}
    LIMIT :limit OFFSET :offset
    """
    result = await database.fetch_all(query, {"userid": userid, "limit": items_per_page, "offset": offset})
    if not result:
        return {"total_pages": total_pages, "data": []}
    
    formatted_result = [{
        "replay_url": r['replayurl'],
        "recording_start": r['recordingstart'],
        "recording_end": r['recordingend'],
        "viewer_count": r['viewercount'],
        "userid": r['userid'],
        "idx": r['idx'],
        "streamname": r['streamname'],
        "nickname": r['nickname'],
        "duration": r['duration']
    } for r in result]

    return {"total_pages": total_pages, "data": formatted_result}

@app.post("/api/user/{userid}/vod")
async def post_replay_info(userid: str, page: int = Query(1, gt=0), sort: str = Query('latest', enum=['latest', 'popular'])):
    items_per_page = 20
    offset = (page - 1) * items_per_page

    if sort == 'latest':
        order_clause = "ORDER BY idx DESC"
    elif sort == 'popular':
        order_clause = "ORDER BY viewercount DESC"

    # 데이터 개수를 계산하는 쿼리
    count_query = "SELECT COUNT(userid) AS count FROM vodview WHERE userid = :userid"
    total_count_result = await database.fetch_one(count_query, {"userid": userid})
    total_count = total_count_result['count']

    # 페이지 수 계산
    total_pages = (total_count + items_per_page - 1) // items_per_page

    query = f"""
    SELECT replayurl, recordingstart, recordingend, viewercount, userid, idx, streamname, nickname, duration
    FROM vodview
    WHERE userid = :userid
    {order_clause}
    LIMIT :limit OFFSET :offset
    """
    result = await database.fetch_all(query, {"userid": userid, "limit": items_per_page, "offset": offset})
    if not result:
        return {"total_pages": total_pages, "data": []}
    
    formatted_result = [{
        "replay_url": r['replayurl'],
        "recording_start": r['recordingstart'],
        "recording_end": r['recordingend'],
        "viewer_count": r['viewercount'],
        "userid": r['userid'],
        "idx": r['idx'],
        "streamname": r['streamname'],
        "nickname": r['nickname'],
        "duration": r['duration']
    } for r in result]

    return {"total_pages": total_pages, "data": formatted_result}

# 다시보기,VOD 상세페이지
@app.post("/api/video/{idx}")
async def post_replay_detail(idx: int):
    # 조회수 업데이트 
    update_query = """
    UPDATE replay SET viewercount = viewercount + 1 WHERE idx = :idx
    """
    await database.execute(update_query, {"idx": idx})

    # 상세 정보 조회
    select_query = """
    SELECT replayurl, recordingstart, recordingend, viewercount, userid, idx, streamname, nickname, userlogo, duration
    FROM godview
    WHERE idx = :idx
    """
    result = await database.fetch_one(select_query, {"idx": idx})
    if result is None:
        return []
    return {
        "replay_url": result['replayurl'],
        "recording_start": result['recordingstart'],
        "recording_end": result['recordingend'],
        "viewer_count": result['viewercount'],
        "userid": result['userid'],
        "idx": result['idx'],
        "nickname": result['nickname'],
        "userlogo": result['userlogo'],
        "duration": result['duration'],
        "streamname": result['streamname']
    }

# 최근 저장된 영상 전체 다시보기 정보 20개
@app.get("/api/replay/recent")
async def get_recent_replay():
    query = """
    SELECT idx, userid, channelid, replayurl, recordingstart, recordingend, viewercount, streamname, nickname, userlogo
    FROM godview
    ORDER BY idx DESC
    LIMIT 20
    """
    result = await database.fetch_all(query)
    if not result:
        return []
    return result
