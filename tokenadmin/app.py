import asyncio
import aiohttp
import json
import random
import string
import logging
import os
import functools

from asyncbb.database import prepare_database, create_pool
from asyncbb.log import configure_logger, log as asyncbb_log

from sanic import Sanic
from sanic.exceptions import SanicException
from sanic.log import log as sanic_log
from sanic.response import html, json as json_response, redirect
from sanic.request import Request
from jinja2 import Environment, FileSystemLoader

from tokenbrowser.utils import parse_int

asyncbb_log.setLevel(logging.DEBUG)
configure_logger(sanic_log)

ADMIN_SERVICE_DATABASE_URL = os.getenv("DATABASE_URL")
ID_SERVICE_LOGIN_URL = os.getenv("ID_SERVICE_LOGIN_URL")

LIVE_ETHEREUM_NODE_URL = os.getenv("LIVE_ETHEREUM_NODE_URL")
LIVE_ETH_SERVICE_DATABASE_URL = os.getenv("LIVE_ETH_SERVICE_DATABASE_URL")
LIVE_ID_SERVICE_DATABASE_URL = os.getenv("LIVE_ID_SERVICE_DATABASE_URL")
LIVE_ID_SERVICE_URL = os.getenv("LIVE_ID_SERVICE_URL")
LIVE_ETH_SERVICE_URL = os.getenv("LIVE_ETH_SERVICE_URL")

DEV_ETHEREUM_NODE_URL = os.getenv("DEV_ETHEREUM_NODE_URL")
DEV_ETH_SERVICE_DATABASE_URL = os.getenv("DEV_ETH_SERVICE_DATABASE_URL")
DEV_ID_SERVICE_DATABASE_URL = os.getenv("DEV_ID_SERVICE_DATABASE_URL")
DEV_ID_SERVICE_URL = os.getenv("DEV_ID_SERVICE_URL")
DEV_ETH_SERVICE_URL = os.getenv("DEV_ETH_SERVICE_URL")

class _Pools:
    def __init__(self, eth_db_pool, id_db_pool):
        self.eth = eth_db_pool
        self.id = id_db_pool

class _Urls:
    def __init__(self, node_url, id_service_url, eth_service_url):
        self.node = node_url
        self.id = id_service_url
        self.eth = eth_service_url

class Config:
    def __init__(self, name, eth_db_pool, id_db_pool, node_url, id_service_url, eth_service_url):
        self.name = name
        self.db = _Pools(eth_db_pool, id_db_pool)
        self.urls = _Urls(node_url, id_service_url, eth_service_url)

def add_config(fn):
    async def wrapper(request, *args, **kwargs):
        if request.path.startswith("/live"):
            config = app.configs['live']
        elif request.path.startswith("/dev"):
            config = app.configs['dev']
        else:
            raise SanicException("Not Found", status_code=404)
        return await fn(request, config, *args, **kwargs)
    return wrapper

class App(Sanic):

    def run(self, *args, **kwargs):
        before_start = kwargs.pop('before_start', None)
        async def prepare_db(app, loop):
            app.configs = {}

            app.pool = adminpool = await prepare_database({'dsn': ADMIN_SERVICE_DATABASE_URL})
            # live
            live_eth = await create_pool(LIVE_ETH_SERVICE_DATABASE_URL, min_size=1, max_size=3)
            live_id = await create_pool(LIVE_ID_SERVICE_DATABASE_URL, min_size=1, max_size=3)
            app.configs['live'] = Config("live", live_eth, live_id,
                                         LIVE_ETHEREUM_NODE_URL,
                                         LIVE_ID_SERVICE_URL,
                                         LIVE_ETH_SERVICE_URL)

            # dev
            dev_eth = await create_pool(DEV_ETH_SERVICE_DATABASE_URL, min_size=1, max_size=3)
            dev_id = await create_pool(DEV_ID_SERVICE_DATABASE_URL, min_size=1, max_size=3)
            app.configs['dev'] = Config("dev", dev_eth, dev_id,
                                        DEV_ETHEREUM_NODE_URL,
                                        DEV_ID_SERVICE_URL,
                                        DEV_ETH_SERVICE_URL)

            # configure http client
            app.http = aiohttp.ClientSession()
            if before_start:
                f = before_start()
                if asyncio.iscoroutine(f):
                    await f
        return super().run(*args, before_start=prepare_db, **kwargs)

    def route(self, uri, methods=frozenset({'GET'}), host=None, prefixed=False):
        if not uri.startswith('/'):
            uri = '/' + uri
        if prefixed:
            def response(handler):
                handler = add_config(handler)
                self.router.add(uri="/live{}".format(uri), methods=methods, handler=handler,
                                host=host)
                self.router.add(uri="/dev{}".format(uri), methods=methods, handler=handler,
                                host=host)
            return response
        else:
            return super().route(uri, methods=methods, host=host)

# monkey patch in path for old versions of sanic
if not hasattr(Request, 'path'):
    from httptools import parse_url

    @property
    def path_monkey_patch(self):
        return self.url

    Request.path = path_monkey_patch

app = App()
env = Environment(enable_async=True, loader=FileSystemLoader('templates'))
env.filters['parse_int'] = parse_int
env.globals.update({'max': max, 'min': min})

def to_eth(wei):
    wei = str(parse_int(wei))
    pad = 17 - len(wei)
    if pad < 0:
        eth = wei[:abs(pad)] + "." + wei[abs(pad):]
    else:
        eth = "0." + wei.zfill(17)
    while eth.endswith("0"):
        eth = eth[:-1]
    if eth.endswith("."):
        eth += "0"
    return eth
env.filters['to_eth'] = to_eth

app.static('/public', './public')
app.static('/favicon.ico', './public/favicon.ico')

def fix_avatar_for_user(id_service_url, user):
    if not user['avatar']:
        user['avatar'] = "{}/identicon/{}.png".format(id_service_url, user['token_id'])
    elif user['avatar'].startswith('/'):
        user['avatar'] = "{}{}".format(
        id_service_url,
            user['avatar'])
    return user

async def get_token_user_from_payment_address(conf, address):
    async with conf.db.id.acquire() as con:
        rows = await con.fetch("SELECT * FROM users WHERE payment_address = $1", address)

    if rows:
        return fix_avatar_for_user(conf.urls.id, dict(rows[0]))

    return None

def generate_session_id():
    return ''.join([random.choices(string.digits + string.ascii_letters)[0] for x in range(32)])

def requires_login(fn):
    async def check_login(request, *args, **kwargs):
        session_cookie = request.cookies.get('session')
        if session_cookie:
            async with app.pool.acquire() as con:
                admin = await con.fetchrow("SELECT admins.token_id FROM admins "
                                           "JOIN sessions ON admins.token_id = sessions.token_id "
                                           "WHERE sessions.session_id = $1",
                                           session_cookie)
            if admin:
                url = '{}/v1/user/{}'.format(ID_SERVICE_LOGIN_URL, admin['token_id'])
                resp = await app.http.get(url)
                if resp.status == 200:
                    admin = await resp.json()
                    if admin['custom']['avatar'].startswith('/'):
                        admin['custom']['avatar'] = "{}{}".format(ID_SERVICE_LOGIN_URL, admin['custom']['avatar'])
                else:
                    admin = None
        else:
            admin = None
        if not admin:
            return redirect("/login?redirect={}".format(request.path))
        # keep the config object as the first argument
        if len(args) and isinstance(args[0], Config):
            args = (args[0], admin, *args[1:])
        else:
            args = (admin, *args)
        rval = await fn(request, *args, **kwargs)
        return rval
    return check_login

@app.route("/")
@requires_login
async def index(request, user):
    return redirect("/live")

@app.route("/", prefixed=True)
@requires_login
async def liveordev(request, conf, user):
    print(conf, user)
    return html(await env.get_template("index.html").render_async(current_user=user, environment=conf.name, page="home"))


@app.get("/login")
async def get_login(request):
    return html(await env.get_template("login.html").render_async())

@app.post("/login")
async def post_login(request):
    token = request.json['auth_token']
    url = '{}/v1/login/verify/{}'.format(ID_SERVICE_LOGIN_URL, token)
    resp = await app.http.get(url)
    if resp.status != 200:
        raise SanicException("Login Failed", status_code=401)

    user = await resp.json()
    token_id = user['token_id']
    session_id = generate_session_id()
    async with app.pool.acquire() as con:
        admin = await con.fetchrow("SELECT * FROM admins WHERE token_id = $1", token_id)
        if admin:
            await con.execute("INSERT INTO sessions (session_id, token_id) VALUES ($1, $2)",
                              session_id, token_id)
    if admin:
        response = json_response(user)
        response.cookies['session'] = session_id
        #response.cookies['session']['secure'] = True
        return response
    else:
        raise SanicException("Login Failed", status_code=401)

@app.route("/txs", prefixed=True)
@requires_login
async def get_txs(request, conf, user):
    page = parse_int(request.args.get('page', None)) or 1
    if page < 1:
        page = 1
    limit = 10
    offset = (page - 1) * limit
    async with conf.db.eth.acquire() as con:
        rows = await con.fetch(
            "SELECT * FROM transactions ORDER BY created DESC OFFSET $1 LIMIT $2",
            offset, limit)
        count = await con.fetchrow(
            "SELECT COUNT(*) FROM transactions")
    txs = []
    for row in rows:
        tx = dict(row)
        tx['from_user'] = await get_token_user_from_payment_address(conf, tx['from_address'])
        tx['to_user'] = await get_token_user_from_payment_address(conf, tx['to_address'])
        txs.append(tx)

    total_pages = count['count'] // limit

    return html(await env.get_template("txs.html").render_async(txs=txs, current_user=user, environment=conf.name, page="txs",
                                                                total=count['count'], total_pages=total_pages, current_page=page))

@app.route("/tx/<tx_hash>", prefixed=True)
@requires_login
async def get_tx(request, conf, current_user, tx_hash):
    context = {'current_user': current_user, 'hash': tx_hash, 'environment': conf.name, 'page': 'txs'}
    async with conf.db.eth.acquire() as con:
        row = await con.fetchrow(
            "SELECT * FROM transactions WHERE transaction_hash = $1",
            tx_hash)
        bnum = await con.fetchrow(
            "SELECT blocknumber FROM last_blocknumber")
    if row:
        context['db'] = row
    if bnum:
        context['block_number'] = bnum['blocknumber']

    resp = await app.http.post(
        conf.urls.node,
        headers={'Content-Type': 'application/json'},
        data=json.dumps({
            "jsonrpc": "2.0",
            "id": random.randint(0, 1000000),
            "method": "eth_getTransactionByHash",
            "params": [tx_hash]
        }).encode('utf-8'))
    if resp.status == 200:
        data = await resp.json()
        if 'result' in data:
            if data['result'] is not None:
                context['node'] = data['result']
        elif 'error' in data:
            context['error'] = data['error']
    else:
        context['error'] = 'Unexpected {} response from node'.format(resp.status)

    if 'node' in context and context['node']['blockNumber'] is not None:
        resp = await app.http.post(
            conf.urls.node,
            headers={'Content-Type': 'application/json'},
            data=json.dumps({
                "jsonrpc": "2.0",
                "id": random.randint(0, 1000000),
                "method": "eth_getTransactionReceipt",
                "params": [tx_hash]
            }).encode('utf-8'))
        data = await resp.json()
        if 'result' in data:
            context['receipt'] = data['result']

    if 'node' in context or 'db' in context:
        from_address = context['node']['from'] if 'node' in context else context['db']['from_address']
        to_address = context['node']['to'] if 'node' in context else context['db']['to_address']

        context['from_user'] = await get_token_user_from_payment_address(conf, from_address)
        context['to_user'] = await get_token_user_from_payment_address(conf, to_address)

    return html(await env.get_template("tx.html").render_async(**context))

@app.route("/users", prefixed=True)
@requires_login
async def get_users(request, conf, current_user):
    page = parse_int(request.args.get('page', None)) or 1
    if page < 1:
        page = 1
    limit = 10
    offset = (page - 1) * limit
    async with conf.db.id.acquire() as con:
        rows = await con.fetch(
            "SELECT * FROM users ORDER BY created DESC OFFSET $1 LIMIT $2",
            offset, limit)
        count = await con.fetchrow(
            "SELECT COUNT(*) FROM users")
    users = []
    for row in rows:
        usr = fix_avatar_for_user(conf.urls.id, dict(row))
        url = '{}/v1/balance/{}'.format(conf.urls.eth, usr['payment_address'])
        resp = await app.http.get(url)
        if resp.status == 200:
            usr['balance'] = await resp.json()

        users.append(usr)

    total_pages = count['count'] // limit

    return html(await env.get_template("users.html").render_async(
        users=users, current_user=current_user, environment=conf.name, page="users",
        total=count['count'], total_pages=total_pages, current_page=page))

@app.route("/user/<token_id>", prefixed=True)
@requires_login
async def get_user(request, conf, current_user, token_id):
    async with conf.db.id.acquire() as con:
        row = await con.fetchrow(
            "SELECT * FROM users WHERE token_id = $1", token_id)
    if not row:
        return html(await env.get_template("user.html").render_async(current_user=current_user))
    usr = fix_avatar_for_user(conf.urls.id, dict(row))
    url = '{}/v1/balance/{}'.format(conf.urls.eth, usr['payment_address'])
    resp = await app.http.get(url)
    if resp.status == 200:
        usr['balance'] = await resp.json()

    async with conf.db.eth.acquire() as con:
        txrows = await con.fetch(
            "SELECT * FROM transactions WHERE from_address = $3 OR to_address = $3 ORDER BY created DESC OFFSET $1 LIMIT $2",
            0, 10, usr['payment_address'])
    txs = []
    for txrow in txrows:
        tx = dict(txrow)
        if tx['from_address'] != usr['payment_address']:
            tx['from_user'] = await get_token_user_from_payment_address(conf, tx['from_address'])
        else:
            tx['from_user'] = usr
        if tx['to_address'] != usr['payment_address']:
            tx['to_user'] = await get_token_user_from_payment_address(conf, tx['to_address'])
        else:
            tx['to_user'] = usr
        txs.append(tx)

    return html(await env.get_template("user.html").render_async(
        user=usr, txs=txs, current_user=current_user, environment=conf.name, page="users"))
