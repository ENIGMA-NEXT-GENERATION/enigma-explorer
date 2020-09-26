#!/usr/bin/env python3

import flask
from datetime import datetime, timedelta
import babel.dates
import json
import sys
import statistics
import string
from base64 import b32encode, b16decode
from werkzeug.routing import BaseConverter
from pygments import highlight
from pygments.lexers import JsonLexer
from pygments.formatters import HtmlFormatter
import subprocess

import config
from lmq import FutureJSON, lmq_connection

# Make a dict of config.* to pass to templating
conf = {x: getattr(config, x) for x in dir(config) if not x.startswith('__')}

git_rev = subprocess.run(["git", "rev-parse", "--short=9", "HEAD"], stdout=subprocess.PIPE, text=True)
if git_rev.returncode == 0:
    git_rev = git_rev.stdout.strip()
else:
    git_rev = "(unknown)"

app = flask.Flask(__name__)

class Hex64Converter(BaseConverter):
    def __init__(self, url_map):
        super().__init__(url_map)
        self.regex = "[0-9a-fA-F]{64}"

app.url_map.converters['hex64'] = Hex64Converter


@app.template_filter('format_datetime')
def format_datetime(value, format='long'):
    return babel.dates.format_datetime(value, format, tzinfo=babel.dates.get_timezone('UTC'))

@app.template_filter('from_timestamp')
def from_timestamp(value):
    return datetime.fromtimestamp(value)

@app.template_filter('ago')
def datetime_ago(value):
    delta = datetime.now() - value
    disp=''
    if delta.days < 0:
        delta = -delta
        disp += '-'
    if delta.days > 0:
        disp += '{}d '.format(delta.days)
    disp += '{:d}:{:02d}:{:02d}'.format(delta.seconds // 3600, delta.seconds // 60 % 60, delta.seconds % 60)
    return disp


@app.template_filter('reltime')
def relative_time(seconds, two_part=False, in_ago=True, neg_is_now=False):
    if isinstance(seconds, timedelta):
        seconds = seconds.seconds + 86400*seconds.days

    ago = False
    if seconds == 0 or (neg_is_now and seconds < 0):
        return 'now'
    elif seconds < 0:
        seconds = -seconds
        ago = True

    if two_part:
        if seconds < 3600:
            delta = '{:.0f} minutes {:.0f} seconds'.format(seconds//60, seconds%60//1)
        elif seconds < 24 * 3600:
            delta = '{:.0f} hours {:.1f} minutes'.format(seconds//3600, seconds%3600/60)
        elif seconds < 10 * 86400:
            delta = '{:.0f} days {:.1f} hours'.format(seconds//86400, seconds%86400/3600)
        else:
            delta = '{:.1f} days'.format(seconds / 86400)
    elif seconds < 90:
        delta = '{:.0f} seconds'.format(seconds)
    elif seconds < 90 * 60:
        delta = '{:.1f} minutes'.format(seconds / 60)
    elif seconds < 36 * 3600:
        delta = '{:.1f} hours'.format(seconds / 3600)
    elif seconds < 99.5 * 86400:
        delta = '{:.1f} days'.format(seconds / 86400)
    else:
        delta = '{:.0f} days'.format(seconds / 86400)

    return delta if not in_ago else delta + ' ago' if ago else 'in ' + delta


@app.template_filter('roundish')
def filter_round(value):
    return ("{:.0f}" if value >= 100 or isinstance(value, int) else "{:.1f}" if value >= 10 else "{:.2f}").format(value)

@app.template_filter('chop0')
def filter_chop0(value):
    value = str(value)
    if '.' in value:
        return value.rstrip('0').rstrip('.')
    return value

si_suffix = ['', 'k', 'M', 'G', 'T', 'P', 'E', 'Z', 'Y']
@app.template_filter('si')
def format_si(value):
    i = 0
    while value >= 1000 and i < len(si_suffix) - 1:
        value /= 1000
        i += 1
    return filter_round(value) + '{}'.format(si_suffix[i])

@app.template_filter('loki')
def format_loki(atomic, tag=True, fixed=False, decimals=9, zero=None):
    """Formats an atomic current value as a human currency value.
    tag - if False then don't append " LOKI"
    fixed - if True then don't strip insignificant trailing 0's and '.'
    decimals - at how many decimal we should round; the default is full precision
    fixed - if specified, replace 0 with this string
    """
    if atomic == 0 and zero:
        disp = zero
    else:
        disp = "{{:.{}f}}".format(decimals).format(atomic * 1e-9)
        if not fixed and decimals > 0:
            disp = disp.rstrip('0').rstrip('.')
    if tag:
        disp += ' LOKI'
    return disp

# For some inexplicable reason some hex fields are provided as array of byte integer values rather
# than hex.  This converts such a monstrosity to hex.
@app.template_filter('bytes_to_hex')
def bytes_to_hex(b):
    return "".join("{:02x}".format(x) for x in b)

@app.template_filter('base32z')
def base32z(hex):
    return b32encode(b16decode(hex, casefold=True)).translate(
            bytes.maketrans(
                b'ABCDEFGHIJKLMNOPQRSTUVWXYZ234567',
                b'ybndrfg8ejkmcpqxot1uwisza345h769')).decode().rstrip('=')


@app.after_request
def add_global_headers(response):
    if 'Cache-Control' not in response.headers:
        response.headers['Cache-Control'] = 'no-store'
    return response

@app.route('/style.css')
def css():
    return flask.send_from_directory('static', 'style.css')


def get_sns_future(lmq, lokid):
    return FutureJSON(lmq, lokid, 'rpc.get_service_nodes', 5,
            args={
                'all': False,
                'fields': { x: True for x in ('service_node_pubkey', 'requested_unlock_height', 'last_reward_block_height',
                    'last_reward_transaction_index', 'active', 'funded', 'earned_downtime_blocks',
                    'service_node_version', 'contributors', 'total_contributed', 'total_reserved',
                    'staking_requirement', 'portions_for_operator', 'operator_address', 'pubkey_ed25519',
                    'last_uptime_proof', 'service_node_version', 'state_height') } })

def get_sns(sns_future, info_future):
    info = info_future.get()
    awaiting_sns, active_sns, inactive_sns = [], [], []
    sn_states = sns_future.get()
    sn_states = sn_states['service_node_states'] if 'service_node_states' in sn_states else []
    for sn in sn_states:
        sn['contribution_open'] = sn['staking_requirement'] - sn['total_reserved']
        sn['contribution_required'] = sn['staking_requirement'] - sn['total_contributed']
        sn['num_contributions'] = sum(len(x['locked_contributions']) for x in sn['contributors'] if 'locked_contributions' in x)

        if sn['active']:
            active_sns.append(sn)
        elif sn['funded']:
            sn['decomm_blocks_remaining'] = max(sn['earned_downtime_blocks'], 0)
            sn['decomm_blocks'] = info['height'] - sn['state_height']
            inactive_sns.append(sn)
        else:
            awaiting_sns.append(sn)
    return awaiting_sns, active_sns, inactive_sns

@app.context_processor
def template_globals():
    return {
        'config': conf,
        'server': {
            'datetime': datetime.utcnow(),
            'timestamp': datetime.utcnow().timestamp(),
            'revision': git_rev,
        },
    }


@app.route('/page/<int:page>')
@app.route('/page/<int:page>/<int:per_page>')
@app.route('/range/<int:first>/<int:last>')
@app.route('/autorefresh/<int:refresh>')
@app.route('/')
def main(refresh=None, page=0, per_page=None, first=None, last=None):
    lmq, lokid = lmq_connection()
    inforeq = FutureJSON(lmq, lokid, 'rpc.get_info', 1)
    stake = FutureJSON(lmq, lokid, 'rpc.get_staking_requirement', 10)
    base_fee = FutureJSON(lmq, lokid, 'rpc.get_fee_estimate', 10)
    hfinfo = FutureJSON(lmq, lokid, 'rpc.hard_fork_info', 10)
    mempool = FutureJSON(lmq, lokid, 'rpc.get_transaction_pool', 5, args={"tx_extra":True})
    sns = get_sns_future(lmq, lokid)
    checkpoints = FutureJSON(lmq, lokid, 'rpc.get_checkpoints', args={"count": 3})

    # This call is slow the first time it gets called in lokid but will be fast after that, so call
    # it with a very short timeout.  It's also an admin-only command, so will always fail if we're
    # using a restricted RPC interface.
    coinbase = FutureJSON(lmq, lokid, 'admin.get_coinbase_tx_sum', 10, timeout=1, fail_okay=True,
            args={"height":0, "count":2**31-1})

    custom_per_page = ''
    if per_page is None or per_page <= 0 or per_page > config.max_blocks_per_page:
        per_page = config.blocks_per_page
    else:
        custom_per_page = '/{}'.format(per_page)

    # We have some chained request dependencies here and below, so get() them as needed; all other
    # non-dependent requests should already have a future initiated above so that they can
    # potentially run in parallel.
    info = inforeq.get()
    height = info['height']

    # Permalinked block range:
    if first is not None and last is not None and 0 <= first <= last and last <= first + 99:
        start_height, end_height = first, last
        if end_height - start_height + 1 != per_page:
            per_page = end_height - start_height + 1;
            custom_per_page = '/{}'.format(per_page)
        # We generally can't get a perfect page number because our range (e.g. 5-14) won't line up
        # with pages (e.g. 10-19, 0-19), so just get as close as we can.  Next/Prev page won't be
        # quite right, but they'll be within half a page.
        page = round((height - 1 - end_height) / per_page)
    else:
        end_height = max(0, height - per_page*page - 1)
        start_height = max(0, end_height - per_page + 1)

    blocks = FutureJSON(lmq, lokid, 'rpc.get_block_headers_range', cache_key='main', args={
        'start_height': start_height,
        'end_height': end_height,
        'get_tx_hashes': True,
        }).get()['headers']

    # If 'txs' is already there then it is probably left over from our cached previous call through
    # here.
    if blocks and 'txs' not in blocks[0]:
        txids = []
        for b in blocks:
            b['txs'] = []
            txids.append(b['miner_tx_hash'])
            if 'tx_hashes' in b:
                txids += b['tx_hashes']
        txs = FutureJSON(lmq, lokid, 'rpc.get_transactions', args={
            "txs_hashes": txids,
            "decode_as_json": True,
            "tx_extra": True,
            "prune": True,
            }).get()
        txs = txs['txs']
        i = 0
        for tx in txs:
            # TXs should come back in the same order so we can just skip ahead one when the block
            # height changes rather than needing to search for the block
            if blocks[i]['height'] != tx['block_height']:
                i += 1
                while i < len(blocks) and blocks[i]['height'] != tx['block_height']:
                    print("Something getting wrong: missing txes?", file=sys.stderr)
                    i += 1
                if i >= len(blocks):
                    print("Something getting wrong: have leftover txes")
                    break
            tx['info'] = json.loads(tx['as_json'])
            blocks[i]['txs'].append(tx)

    
    # mempool RPC return values are about as nasty as can be.  For each mempool tx, we get back
    # *both* binary+hex encoded values and JSON-encoded values slammed into a string, which means we
    # have to invoke an *extra* JSON parser for each tx.  This is terrible.
    mp = mempool.get()
    if 'transactions' in mp:
        for tx in mp['transactions']:
            tx['info'] = json.loads(tx["tx_json"])
    else:
        mp['transactions'] = []

    # Clean up the SN data a bit to make things easier for the templates
    awaiting_sns, active_sns, inactive_sns = get_sns(sns, inforeq)

    return flask.render_template('index.html',
            info=info,
            stake=stake.get(),
            fees=base_fee.get(),
            emission=coinbase.get(),
            hf=hfinfo.get(),
            active_sns=active_sns,
            inactive_sns=inactive_sns,
            awaiting_sns=awaiting_sns,
            blocks=blocks,
            block_size_median=statistics.median(b['block_size'] for b in blocks),
            page=page,
            per_page=per_page,
            custom_per_page=custom_per_page,
            mempool=mp,
            checkpoints=checkpoints.get(),
            refresh=refresh,
            )

@app.route('/service_nodes')
def sns():
    lmq, lokid = lmq_connection()
    info = FutureJSON(lmq, lokid, 'rpc.get_info', 1)
    awaiting, active, inactive = get_sns(get_sns_future(lmq, lokid), info)

    return flask.render_template('service_nodes.html',
        info=info.get(),
        active_sns=active,
        awaiting_sns=awaiting,
        inactive_sns=inactive,
        )

def tx_req(lmq, lokid, txid, **kwargs):
    return FutureJSON(lmq, lokid, 'rpc.get_transactions', cache_seconds=10, cache_key='single',
            args={
                "txs_hashes": [txid],
                "decode_as_json": True,
                "tx_extra": True,
                "prune": True,
                },
            **kwargs)

def sn_req(lmq, lokid, pubkey, **kwargs):
    return FutureJSON(lmq, lokid, 'rpc.get_service_nodes', 5, cache_key='single',
            args={"service_node_pubkeys": [pubkey]}, **kwargs
        )

def block_req(lmq, lokid, hash_or_height, **kwargs):
    if len(hash_or_height) <= 10 and hash_or_height.isdigit():
        return FutureJSON(lmq, lokid, 'rpc.get_block_header_by_height', cache_key='single',
                args={ "height": int(hash_or_height) }, **kwargs)
    else:
        return FutureJSON(lmq, lokid, 'rpc.get_block_header_by_hash', cache_key='single',
                args={ 'hash': hash_or_height }, **kwargs)

def get_block_with_txs(lmq, lokid, hash_or_height, **kwargs):
    if len(hash_or_height) <= 10 and hash_or_height.isdigit():
        return FutureJSON(lmq, lokid, 'rpc.get_block', cache_key='single',
                args={ "height": int(hash_or_height), 'get_tx_hashes': True }, **kwargs)
    else:
        return FutureJSON(lmq, lokid, 'rpc.get_block', cache_key='single',
                args={ 'hash': hash_or_height, 'get_tx_hashes': True }, **kwargs)
    

@app.route('/service_node/<hex64:pubkey>')  # For backwards compatibility with old explorer URLs
@app.route('/sn/<hex64:pubkey>')
def show_sn(pubkey):
    lmq, lokid = lmq_connection()
    info = FutureJSON(lmq, lokid, 'rpc.get_info', 1)
    hfinfo = FutureJSON(lmq, lokid, 'rpc.hard_fork_info', 10)
    sn = sn_req(lmq, lokid, pubkey).get()

    if 'service_node_states' not in sn or not sn['service_node_states']:
        return flask.render_template('not_found.html',
                info=info.get(),
                type='sn',
                id=pubkey,
                )

    sn = sn['service_node_states'][0]
    # These are a bit non-trivial to properly calculate:

    # Number of staked contributions
    sn['num_contributions'] = sum(len(x["locked_contributions"]) for x in sn["contributors"] if "locked_contributions" in x)
    # Number of unfilled, reserved contribution spots:
    sn['num_reserved_spots'] = sum(x["amount"] < x["reserved"] for x in sn["contributors"])
    # Available open contribution spots:
    sn['num_open_spots'] = 0 if sn['total_reserved'] >= sn['staking_requirement'] else max(0, 4 - sn['num_contributions'] - sn['num_reserved_spots'])

    return flask.render_template('sn.html',
            info=info.get(),
            hf=hfinfo.get(),
            sn=sn,
            )


@app.route("/block/<val>")
def show_block(val):
    """ """
    lmq, lokid = lmq_connection()
    block = get_block_with_txs(lmq, lokid, val).get()
    info = FutureJSON(lmq, lokid, 'rpc.get_info', 1).get()
    hfinfo = FutureJSON(lmq, lokid, 'rpc.hard_fork_info', 10)
    if block is None:
        return flask.render_template("not_found.html",
                                     info=info,
                                     hfinfo=hfinfo.get(),
                                     type='block')
    else:
        next_block = None
        block_height = block['block_header']['height']
        transactions = []
        miner_txs = []
        if block and 'tx_hashes' in block:
            hashes = block['tx_hashes']
            if 'info' not in block:
                try:
                    block['info'] = json.loads(block["json"])
                    del block["json"]
                except:
                    pass
            if 'info' in block:
                hashes += block['info']['miner_tx']
            txs = FutureJSON(lmq, lokid, 'rpc.get_transactions', args={
                "txs_hashes": hashes,
                "decode_as_json": True
            }).get()
            for tx in txs['txs']:
                if 'info' not in tx:
                    tx['info'] = json.loads(tx["as_json"])
                    del tx["as_json"]
                transactions.append(tx)
        if info['height'] > 1 + block_height:
            next_block = block_req(lmq, lokid, '{}'.format(block_height + 1)).get()
        return flask.render_template("block.html",
                                     info=info,
                                     hfinfo=hfinfo.get(),
                                     block_header=block['block_header'],
                                     block=block,
                                     transactions=transactions,
                                     next_block=next_block)
 
    

@app.route('/tx/<hex64:txid>')
@app.route('/tx/<hex64:txid>/<int:more_details>')
def show_tx(txid, more_details=False):
    lmq, lokid = lmq_connection()
    info = FutureJSON(lmq, lokid, 'rpc.get_info', 1)
    txs = tx_req(lmq, lokid, txid).get()

    if 'txs' not in txs or not txs['txs']:
        return flask.render_template('not_found.html',
                info=info.get(),
                type='tx',
                id=txid,
                )
    tx = txs['txs'][0]
    if 'info' not in tx:
        tx['info'] = json.loads(tx["as_json"])
        del tx["as_json"]

        # The "extra" field is retardedly in per-byte values, convert it to a hex string:
        tx['info']['extra'] = bytes_to_hex(tx['info']['extra'])

    kindex_info = {} # { amount => { keyindex => {output-info} } }
    block_info_req = None
    if 'vin' in tx['info']:
        if len(tx['info']['vin']) == 1 and 'gen' in tx['info']['vin'][0]:
            tx['coinbase'] = True
        elif tx['info']['vin'] and config.enable_mixins_details:
            # Load output details for all outputs contained in the inputs
            outs_req = []
            for inp in tx['info']['vin']:
                # Key positions are stored as offsets from the previous index rather than indices,
                # so de-delta them back into indices:
                if 'key_offsets' in inp['key'] and 'key_indices' not in inp['key']:
                    kis = []
                    inp['key']['key_indices'] = kis
                    kbase = 0
                    for koff in inp['key']['key_offsets']:
                        kbase += koff
                        kis.append(kbase)
                    del inp['key']['key_offsets']

            outs_req = [{"amount":inp['key']['amount'], "index":ki} for inp in tx['info']['vin'] for ki in inp['key']['key_indices']]
            outputs = FutureJSON(lmq, lokid, 'rpc.get_outs', args={
                'get_txid': True,
                'outputs': outs_req,
                }).get()
            if outputs and 'outs' in outputs and len(outputs['outs']) == len(outs_req):
                outputs = outputs['outs']
                # Also load block details for all of those outputs:
                block_info_req = FutureJSON(lmq, lokid, 'rpc.get_block_header_by_height', args={
                    'heights': [o["height"] for o in outputs]
                })
                i = 0
                for inp in tx['info']['vin']:
                    amount = inp['key']['amount']
                    if amount not in kindex_info:
                        kindex_info[amount] = {}
                    ki = kindex_info[amount]
                    for ko in inp['key']['key_indices']:
                        ki[ko] = outputs[i]
                        i += 1

    if more_details:
        formatter = HtmlFormatter(cssclass="syntax-highlight", style="native")
        more_details = {
                'details_css': formatter.get_style_defs('.syntax-highlight'),
                'details_html': highlight(json.dumps(tx, indent="\t", sort_keys=True), JsonLexer(), formatter),
                }
    else:
        more_details = {}

    block_info = {} # { height => {block-info} }
    if block_info_req:
        bi = block_info_req.get()
        if 'block_headers' in bi:
            for bh in bi['block_headers']:
                block_info[bh['height']] = bh

    return flask.render_template('tx.html',
            info=info.get(),
            tx=tx,
            kindex_info=kindex_info,
            block_info=block_info,
            **more_details,
            )


@app.route('/search')
def search():
    lmq, lokid = lmq_connection()
    info = FutureJSON(lmq, lokid, 'rpc.get_info', 1)
    val = flask.request.args.get('value').strip()

    if val and len(val) < 10 and val.isdigit(): # Block height
        return show_block(val)
    if not val or len(val) != 64 or any(c not in string.hexdigits for c in val):
        return flask.render_template('not_found.html',
                info=info.get(),
                type='bad_search',
                id=val,
                )

    # Initiate all the lookups at once, then redirect to whichever one responds affirmatively
    snreq = sn_req(lmq, lokid, val)
    blreq = block_req(lmq, lokid, val, fail_okay=True)
    txreq = tx_req(lmq, lokid, val)

    sn = snreq.get()
    if 'service_node_states' in sn and sn['service_node_states']:
        return flask.redirect(flask.url_for('show_sn', pubkey=val), code=301)
    bl = blreq.get()
    if bl and 'block_header' in bl and bl['block_header']:
        return flask.redirect(flask.url_for('show_block', hash=val), code=301)
    tx = txreq.get()
    if tx and 'txs' in tx and tx['txs']:
        return flask.redirect(flask.url_for('show_tx', txid=val), code=301)

    return flask.render_template('not_found.html',
            info=info.get(),
            type='search',
            id=val,
            )

