#  Polkascan PRE Explorer API
#
#  Copyright 2018-2020 openAware BV (NL).
#  This file is part of Polkascan.
#
#  Polkascan is free software: you can redistribute it and/or modify
#  it under the terms of the GNU General Public License as published by
#  the Free Software Foundation, either version 3 of the License, or
#  (at your option) any later version.
#
#  Polkascan is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU General Public License for more details.
#
#  You should have received a copy of the GNU General Public License
#  along with Polkascan. If not, see <http://www.gnu.org/licenses/>.
#
#  polkascan.py
from hashlib import blake2b

import binascii

import json
import falcon
import pytz
import decimal
from dogpile.cache.api import NO_VALUE
from scalecodec.type_registry import load_type_registry_preset
from sqlalchemy import func, tuple_, or_
from sqlalchemy.orm import defer, subqueryload, lazyload, lazyload_all, Query

from app import settings
from app.models.data import Block, Extrinsic, Event, RuntimeCall, RuntimeEvent, Runtime, RuntimeModule, \
    RuntimeCallParam, RuntimeEventAttribute, RuntimeType, RuntimeStorage, Account, Session, Contract, \
    BlockTotal, SessionValidator, Log, AccountIndex, RuntimeConstant, SessionNominator, \
    RuntimeErrorMessage, SearchIndex, AccountInfoSnapshot, Stats
from app.resources.base import JSONAPIResource, JSONAPIListResource, JSONAPIDetailResource, BaseResource
from app.utils.ss58 import ss58_decode, ss58_encode
from app.utils.jwt_validator import validateToken
from scalecodec.base import RuntimeConfiguration
from substrateinterface import SubstrateInterface

# 1 Billion
METAMUI_TOTAL =  decimal.Decimal("1000000000")


class BlockDetailsResource(JSONAPIDetailResource):

    def get_item_url_name(self):
        return 'block_id'

    def get_item(self, item_id):
        if item_id.isnumeric():
            return Block.query(self.session).filter_by(id=item_id).first()
        else:
            return Block.query(self.session).filter_by(hash=item_id).first()

    def get_relationships(self, include_list, item):
        relationships = {}

        if 'extrinsics' in include_list:
            relationships['extrinsics'] = Extrinsic.query(self.session).filter_by(block_id=item.id).order_by(
                'extrinsic_idx')
        if 'transactions' in include_list:
            relationships['transactions'] = Extrinsic.query(self.session).options(defer('params')).filter_by(block_id=item.id, signed=1).order_by(
                'extrinsic_idx')
        if 'inherents' in include_list:
            relationships['inherents'] = Extrinsic.query(self.session).options(defer('params')).filter_by(block_id=item.id, signed=0).order_by(
                'extrinsic_idx')
        if 'events' in include_list:
            relationships['events'] = Event.query(self.session).filter_by(block_id=item.id).order_by(
                'event_idx')
        if 'logs' in include_list:
            relationships['logs'] = Log.query(self.session).filter_by(block_id=item.id).order_by(
                'log_idx')

        return relationships


class BlockListResource(JSONAPIListResource):

    def get_query(self):
        return Block.query(self.session).order_by(
            Block.id.desc()
        )


class BlockTotalDetailsResource(JSONAPIDetailResource):

    def get_item(self, item_id):
        if item_id.isnumeric():
            return BlockTotal.query(self.session).get(item_id)
        else:
            block = Block.query(self.session).filter_by(hash=item_id).first()
            if block:
                return BlockTotal.query(self.session).get(block.id)

    def serialize_item(self, item, auth_user=False):
        # Exclude large params from list view
        data = item.serialize()

        # Include author account
        if item.author_account:
            data['attributes']['author_account'] = item.author_account.serialize()
        return data

    def serialize_item(self, item):
        # Exclude large params from list view
        data = item.serialize()

        # Include author account
        if item.author_account:
            data['attributes']['author_account'] = item.author_account.serialize()
        return data


class BlockTotalListResource(JSONAPIListResource):

    def get_query(self):
        return BlockTotal.query(self.session).order_by(
            BlockTotal.id.desc()
        )

    def apply_filters(self, query, params):

        if params.get('filter[author]'):
            account_id =  bytearray.fromhex(params.get('filter[author]').replace('0x','')).decode()
            # if len(params.get('filter[author]')) == 64:
            #     account_id = params.get('filter[author]')
            # else:
            #     try:
            #         account_id = ss58_decode(params.get('filter[author]'), settings.SUBSTRATE_ADDRESS_TYPE)
            #     except ValueError:
            #         return query.filter(False)

            query = query.filter_by(author=account_id)

        return query


class ExtrinsicListResource(JSONAPIListResource):

    exclude_params = True

    def get_query(self):
        return Extrinsic.query(self.session).options(defer('params')).order_by(
            Extrinsic.block_id.desc()
        )

    def serialize_item(self, item):
        # Exclude large params from list view

        if self.exclude_params:
            data = item.serialize(exclude=['params'])
        else:
            data = item.serialize()

        # Add account as relationship
        if item.account:
            # data['relationships'] = {'account': {"type": "account", "id": item.account.id}}
            data['attributes']['account'] = item.account.serialize()
        return data

    # def get_included_items(self, items):
    #     # Include account items
    #     return [item.account.serialize() for item in items if item.account]

    def apply_filters(self, query, params):

        if params.get('filter[address]'):
            
            # Since we are storing balance in DID, we need to parse hex to did
            account_id = bytearray.fromhex(params.get('filter[address]').replace('0x','')).decode()
            # if len(params.get('filter[address]')) == 64:
            #     account_id = params.get('filter[address]')
            # else:
            #     try:
            #         account_id = ss58_decode(params.get('filter[address]'), settings.SUBSTRATE_ADDRESS_TYPE)
            #     except ValueError:
            #         return query.filter(False)
        else:
            account_id = None

        if params.get('filter[search_index]'):

            self.exclude_params = False

            if type(params.get('filter[search_index]')) != list:
                params['filter[search_index]'] = [params.get('filter[search_index]')]

            search_index = SearchIndex.query(self.session).filter(
                SearchIndex.index_type_id.in_(params.get('filter[search_index]')),
                SearchIndex.account_id == account_id
            ).order_by(SearchIndex.sorting_value.desc())

            query = query.filter(tuple_(Extrinsic.block_id, Extrinsic.extrinsic_idx).in_(
                [[s.block_id, s.extrinsic_idx] for s in search_index]
            ))
        else:

            self.exclude_params = True

            if params.get('filter[signed]'):

                query = query.filter_by(signed=params.get('filter[signed]'))

            if params.get('filter[module_id]'):

                query = query.filter_by(module_id=params.get('filter[module_id]'))

            if params.get('filter[call_id]'):

                query = query.filter_by(call_id=params.get('filter[call_id]'))

            if params.get('filter[address]'):

                query = query.filter_by(address=account_id)

        return query


class ExtrinsicDetailResource(JSONAPIDetailResource):

    def get_item_url_name(self):
        return 'extrinsic_id'

    def get_item(self, item_id):
        print("Called: 2")
        if item_id[0:2] == '0x':
            extrinsic = Extrinsic.query(self.session).filter_by(extrinsic_hash=item_id[2:]).first()
        else:

            if len(item_id.split('-')) != 2:
                return None

            extrinsic = Extrinsic.query(self.session).get(item_id.split('-'))

        return extrinsic

    def get_relationships(self, include_list, item):
        relationships = {}

        if 'events' in include_list:
            relationships['events'] = Event.query(self.session).filter_by(
                block_id=item.block_id,
                extrinsic_idx=item.extrinsic_idx
            ).order_by('event_idx')

        return relationships

    def check_params(self, params, identifier):
        for idx, param in enumerate(params):

            if 'value' in param and 'type' in param:

                if type(param['value']) is list:
                    param['value'] = self.check_params(param['value'], identifier)

                else:
                    if param['type'] == 'Box<Call>':
                        param['value']['call_args'] = self.check_params(param['value']['call_args'], identifier)

                    elif type(param['value']) is str and len(param['value']) > 200000:
                        param['value'] = "{}/{}".format(
                            identifier,
                            blake2b(bytes.fromhex(param['value'].replace('0x', '')), digest_size=32).digest().hex()
                        )
                        param["type"] = "DownloadableBytesHash"
                        param['valueRaw'] = ""

        return params

    def serialize_item(self, item, auth=False):
        data = item.serialize()

        runtime_call = RuntimeCall.query(self.session).filter_by(
            module_id=item.module_id,
            call_id=item.call_id,
            spec_version=item.spec_version_id
        ).first()

        data['attributes']['documentation'] = runtime_call.documentation

        block = Block.query(self.session).get(item.block_id)

        data['attributes']['datetime'] = block.datetime.replace(tzinfo=pytz.UTC).isoformat()

        if item.account:
            data['attributes']['account'] = item.account.serialize()

        if item.params:
            item.params = self.check_params(item.params, item.serialize_id())

        if item.module_id == 'balances' and item.call_id=='transfer':
            event_data = Event.query(self.session).filter_by(
                block_id=item.block_id,
                event_id='Transfer',
                extrinsic_idx=item.extrinsic_idx
            ).first()
            if event_data:
                print("transfer event data: ", event_data.attributes)
                data['attributes']['event_params']= getFormattedTransferEvent(event_data.attributes, auth)
        if item.module_id == 'balances' and item.call_id=='transfer_with_memo' and len(item.params) >= 2:
            event_data = Event.query(self.session).filter_by(
                block_id=item.block_id,
                event_id='Transfer',
                extrinsic_idx=item.extrinsic_idx
            ).first()
            if event_data:
                print("transfer event data: ", event_data.attributes)
                data['attributes']['event_params']= getFormattedTransferEvent(event_data.attributes, auth, item.params[2])

        if item.error:
            # Retrieve ExtrinsicFailed event
            extrinsic_failed_event = Event.query(self.session).filter_by(
                block_id=item.block_id,
                event_id='ExtrinsicFailed'
            ).first()

            # Retrieve runtime error
            if extrinsic_failed_event:
                if 'Module' in extrinsic_failed_event.attributes[0]['value']:

                    error = RuntimeErrorMessage.query(self.session).filter_by(
                        module_index=extrinsic_failed_event.attributes[0]['value']['Module']['index'],
                        index=extrinsic_failed_event.attributes[0]['value']['Module']['error'],
                        spec_version=item.spec_version_id
                    ).first()

                    if error:
                        data['attributes']['error_message'] = error.documentation
                elif 'BadOrigin' in extrinsic_failed_event.attributes[0]['value']:
                    data['attributes']['error_message'] = 'Bad origin'
                elif 'CannotLookup' in extrinsic_failed_event.attributes[0]['value']:
                    data['attributes']['error_message'] = 'Cannot lookup'

        return data

def getFormattedTransferEvent(event_attribs, auth_user, memo_param=False):
    """
    Formats the given balance transfer event attributes to human readable dict. 
    """
    # balance transfer event will have 4 information
    # 1. sender_did 2. receiver_did 3. amount 4. memo
    sender = ""
    receiver = ""
    amount = ""
    memo = ""
    print("auth_user: ",auth_user)
    for event in event_attribs:
        if event['type'] == 'Did' and event_attribs.index(event) == 0:
            # its a sender
            sender = bytearray.fromhex(event['value'].replace('0x','')).decode().rstrip(' \t\r\n\0')
            # sender = sender[:settings.STR_MASK_LEN].ljust(settings.STR_DID_LEN, "*")
        elif event['type'] == 'Did' and event_attribs.index(event) == 1:
            # its receiver
            receiver = bytearray.fromhex(event['value'].replace('0x','')).decode().rstrip(' \t\r\n\0')
            # receiver = receiver[:settings.STR_MASK_LEN].ljust(settings.STR_DID_LEN, "*")
        elif event['type'] == 'Balance':
            amount = event['value']
    if memo_param:
        print('Found memo!')
        memo = memo_param['value']
    if not auth_user or auth_user not in [sender, receiver]:
        print('User not authenticated')
        sender = sender[:settings.STR_MASK_LEN].ljust(settings.STR_DID_LEN, "*")
        receiver = receiver[:settings.STR_MASK_LEN].ljust(settings.STR_DID_LEN, "*")
    return {
        "sender": sender,
        "receiver": receiver,
        "amount": amount,
        "memo": memo
    }
       

class EventsListResource(JSONAPIListResource):

    def apply_filters(self, query, params):

        if params.get('filter[address]'):
            print('EventsListResource: ', params.get('filter[address]'))
            
            # Since we are storing balance in DID, we need to parse hex to did
            account_id = bytearray.fromhex(params.get('filter[address]').replace('0x','')).decode()
                # if len(params.get('filter[address]')) == 64:
                #     account_id = params.get('filter[address]')
                # else:
                #     try:
                #         account_id = ss58_decode(params.get('filter[address]'), settings.SUBSTRATE_ADDRESS_TYPE)
                #     except ValueError:
                #         return query.filter(False)
        else:
            account_id = None

        if params.get('filter[search_index]'):

            if type(params.get('filter[search_index]')) != list:
                params['filter[search_index]'] = [params.get('filter[search_index]')]

            search_index = SearchIndex.query(self.session).filter(
                SearchIndex.index_type_id.in_(params.get('filter[search_index]')),
                SearchIndex.account_id == account_id
            ).order_by(SearchIndex.sorting_value.desc())

            query = query.filter(tuple_(Event.block_id, Event.event_idx).in_(
                [[s.block_id, s.event_idx] for s in search_index]
            ))
        else:

            if params.get('filter[module_id]'):
                query = query.filter_by(module_id=params.get('filter[module_id]'))

            if params.get('filter[event_id]'):

                query = query.filter_by(event_id=params.get('filter[event_id]'))
            else:
                query = query.filter(Event.event_id.notin_(['ExtrinsicSuccess', 'ExtrinsicFailed']))

        return query

    def get_query(self):
        return Event.query(self.session).order_by(
            Event.block_id.desc()
        )


class EventDetailResource(JSONAPIDetailResource):

    def get_item_url_name(self):
        return 'event_id'

    def get_item(self, item_id):
        if len(item_id.split('-')) != 2:
            return None
        event_data = Event.query(self.session).get(item_id.split('-')) 
        refactor_attribs = []
        # Convert all Did type in events with human readable format
        for attrib in event_data.attributes:
            if attrib['type'] == 'Did':
                attrib['value'] = bytearray.fromhex(attrib['value'].replace('0x','')).decode().rstrip(' \t\r\n\0')
                # Will decide on masking or not in serialization, based on auth status
                # attrib['value'] = s[:settings.STR_MASK_LEN].ljust(settings.STR_DID_LEN, "*")
            refactor_attribs.append(attrib)    
        event_data.attributes = refactor_attribs;        
        return event_data

    def serialize_item(self, item, auth_user=False):
        data = item.serialize()

        runtime_event = RuntimeEvent.query(self.session).filter_by(
            module_id=item.module_id,
            event_id=item.event_id,
            spec_version=item.spec_version_id
        ).first()

        # List all the did's which comes under this event
        event_dids = [x['value'] for x in item.attributes if x['type'] == 'Did']
        print(event_dids)
        # evaluate masking only if event has any DID 
        if event_dids:
            if not auth_user or auth_user not in event_dids:
                print('User not authenticated')
                for attrib in item.attributes:
                    if attrib['type'] == 'Did':
                        attrib['value'] = attrib['value'][:settings.STR_MASK_LEN].ljust(settings.STR_DID_LEN, "*")
        data['attributes']['documentation'] = runtime_event.documentation

        return data


class LogListResource(JSONAPIListResource):

    def get_query(self):
        return Log.query(self.session).order_by(
            Log.block_id.desc()
        )


class LogDetailResource(JSONAPIDetailResource):

    def get_item(self, item_id):
        if len(item_id.split('-')) != 2:
            return None
        return Log.query(self.session).get(item_id.split('-'))

class StatsResource(JSONAPIResource):

    cache_expiration_time = 6

    def on_get(self, req, resp, currency_id='metamui'):
        resp.status = falcon.HTTP_200

        # TODO make caching more generic for custom resources

        cache_key = '{}-{}'.format(req.method, req.url)

        response = self.cache_region.get(cache_key, self.cache_expiration_time)

        if response is NO_VALUE:
            print('metamui stats not exist in cache!')
            # Temporary hack to get the network stats
            # TODO: Fix BlockTotal saving to DB
            stats = Stats.query(self.session).get(currency_id)
            # print("best_block: ",best_block.id)
            if stats:
                response = self.get_jsonapi_response(
                    data={
                        'type': 'currency_stats',
                        'id': currency_id,
                        'attributes': {
                            'currency_id': stats.id,
                            'token_name': stats.token_name,
                            'official_site': stats.site,
                            'currency_decimals': stats.decimals,
                            'current_circulation': stats.current_circulation,
                            'total_supply': stats.total_supply
                        }
                    },
                )
            else:
                response = self.get_jsonapi_response(
                    data={
                        'type': 'currency_stats',
                        'id': currency_id,
                        'attributes': {
                            'currency_id': 'N/A',
                            'token_name': 'N/A',
                            'official_site': 'N/A',
                            'currency_decimals': 'N/A',
                            'current_circulation': 'N/A',
                            'total_supply': 'N/A'
                        }
                    },
                )
            self.cache_region.set(cache_key, response)
            resp.set_header('X-Cache', 'MISS')
        else:
            resp.set_header('X-Cache', 'HIT')

        resp.media = response

class NetworkStatisticsResource(JSONAPIResource):

    cache_expiration_time = 60

    def on_get(self, req, resp, currency_id='metamui'):
        resp.status = falcon.HTTP_200
        cache_key = '{}-{}'.format(req.method, req.url)

        response = self.cache_region.get(cache_key, self.cache_expiration_time)

        if response is NO_VALUE:
            print('metamui stats not exist in cache!')
            stats = Stats.query(self.session).get(currency_id)
            if stats:
                response = self.get_jsonapi_response(
                    data={
                        'type': 'currency_stats',
                        'id': currency_id,
                        'attributes': {
                            'currency_id': stats.id,
                            'currency_name': stats.token_name,
                            'currency_symbol': stats.symbol,
                            'official_site': stats.site,
                            'currency_decimals': stats.decimals,
                            'current_circulation': stats.current_circulation,
                            'total_supply': stats.total_supply
                        }
                    },
                )
            else:
                response = self.get_jsonapi_response(
                    data={
                        'type': 'currency_stats',
                        'id': currency_id,
                        'attributes': {
                            'currency_id': 'N/A',
                            'currency_name': 'N/A',
                            'currency_symbol': 'N/A',
                            'official_site': 'N/A',
                            'currency_decimals': 'N/A',
                            'current_circulation': 'N/A',
                            'total_supply': 'N/A'
                        }
                    },
                )
            self.cache_region.set(cache_key, response)
            resp.set_header('X-Cache', 'MISS')
        else:
            resp.set_header('X-Cache', 'HIT')

        resp.media = response

class MetamuiStatisticsDetailResource(JSONAPIResource):

    cache_expiration_time = 60

    def on_get(self, req, resp, field_id=None):
        resp.status = falcon.HTTP_200
        cache_key = '{}-{}'.format(req.method, req.url)

        response = self.cache_region.get(cache_key, self.cache_expiration_time)

        if response is NO_VALUE:
            print('metamui stats not exist in cache!')
            stats = Stats.query(self.session).get('metamui')
            if stats:
                if field_id == 'total_supply':
                    response = stats.total_supply
                elif field_id == 'current_circulation':
                    response = stats.current_circulation
                else:
                    response = "Requested data not found"
            else:
                response = "Requested data not found"
            self.cache_region.set(cache_key, response)
            resp.set_header('X-Cache', 'MISS')
        else:
            resp.set_header('X-Cache', 'HIT')

        resp.media = response

class BalanceTransferHistoryListResource(JSONAPIListResource):

    def get_query(self):
        return Event.query(self.session).filter(
            Event.module_id == 'balances', Event.event_id == 'Transfer'
        ).order_by(Event.block_id.desc())

    def apply_filters(self, query, params):
        print("query",query)
        print("params",params)
        if params.get('filter[address]'):
            print('BalanceTransferHistoryListResource: ', params.get('filter[address]'))
            if params.get('filter[address]')[0:2] == '0x':
                # Raw DID
                print('BalanceTransferHistory fetch by RAW DID')
                account_id = params.get('filter[address]')
            else:
                # Convert text to hex & append with trailing 0's & hex prefix(0x)
                print('BalanceTransferHistory fetch by DID')
                account_id = '0x{:<064}'.format("".join("{:02x}".format(ord(c)) for c in params.get('filter[address]')))   
                # Just to ensure that the DID is not exceeding the length of 32 bytes
                account_id = account_id[:66] 
            print('BalanceTransferHistoryListResource: account_id', account_id)
            query = Event.query(self.session).filter(Event.attributes.contains([{"$[*].value":[account_id]}])==1).all().order_by(Event.block_id.desc())
            # query = Query(Event).filter(Event.attributes.comparator.contains([account_id], '$[*].value'))

        return query

    def serialize_item(self, item):

        if item.event_id == 'Transfer':
            s = bytearray.fromhex(item.attributes[0]['value'].replace('0x','')).decode().rstrip(' \t\r\n\0')
            sender_data = {
                'type': 'account',
                'id': item.attributes[0]['value'].replace('0x', ''),
                'attributes': {
                    'id': item.attributes[0]['value'].replace('0x', ''),
                    'address': s[:settings.STR_MASK_LEN].ljust(settings.STR_DID_LEN, "*")
                    # 'address': ss58_encode(item.attributes[0]['value'].replace('0x', ''), settings.SUBSTRATE_ADDRESS_TYPE)
                }
            }
            s = bytearray.fromhex(item.attributes[1]['value'].replace('0x','')).decode().rstrip(' \t\r\n\0')
            destination_data = {
                'type': 'account',
                'id': item.attributes[1]['value'].replace('0x', ''),
                'attributes': {
                    'id': item.attributes[1]['value'].replace('0x', ''),
                    'address': s[:settings.STR_MASK_LEN].ljust(settings.STR_DID_LEN, "*")
                    # 'address': ss58_encode(item.attributes[1]['value'].replace('0x', ''), settings.SUBSTRATE_ADDRESS_TYPE)
                }
            }
            # Some networks don't have fees
            if len(item.attributes) == 4:
                fee = item.attributes[3]['value']
            else:
                fee = 0

            value = item.attributes[2]['value']
        elif item.event_id == 'Claimed':

            fee = 0
            sender_data = {'name': 'Claim', 'eth_address': item.attributes[1]['value']}
            destination_data = {}
            value = item.attributes[2]['value']

        elif item.event_id == 'Deposit':

            fee = 0
            sender_data = {'name': 'Deposit'}
            destination_data = {}
            value = item.attributes[1]['value']

        elif item.event_id == 'Reward':
            fee = 0
            sender_data = {'name': 'Staking reward'}
            destination_data = {}
            value = item.attributes[1]['value']
        else:
            sender_data = {}
            fee = 0
            destination_data = {}
            value = None

        return {
            'type': 'balancetransfer',
            'id': '{}-{}'.format(item.block_id, item.extrinsic_idx),
            'attributes': {
                'block_id': item.block_id,
                'event_id': item.event_id,
                'event_idx': '{}-{}'.format(item.block_id, item.extrinsic_idx),
                'sender': sender_data,
                'destination': destination_data,
                'value': value,
                'fee': fee
            }
        }



# TODO: Temp hack. Need lot of refactoring 
class BalanceTransferHistoryDetailResource(JSONAPIResource):
    def on_get(self, req, resp, did=None):
        transfer_data = [] 
        resp.status = falcon.HTTP_200
        auth_user = False
        # print("auth: ",req.auth)
        tokenValidation = validateToken(req.auth)
        print(tokenValidation)
        if tokenValidation and 'did' in tokenValidation:
            auth_user = tokenValidation['did']
        if did:
            print('BalanceTransferHistoryListResource: ', did)
            if did[0:2] == '0x':
                # Raw DID
                print('BalanceTransferHistory fetch by RAW DID')
                account_id = did
            else:
                # Convert text to hex & append with trailing 0's & hex prefix(0x)
                print('BalanceTransferHistory fetch by DID')
                account_id = "0x{:<064}".format("".join("{:02x}".format(ord(c)) for c in did))  
                # For lengthy DID's (Eg: XT's), convert to 32 byte size
                account_id = account_id[:66]
                print("raw_did",account_id)  
            # query = Event.query(self.session).filter(func.json_contains({'attributes':[{'value':account_id}]})).all()
            # query = self.session().query(Event).filter(Event.attributes.contains({'value': account_id}))
            # query = Event.query(self.session).filter(func.json_contains(Event.attributes,json.dumps(account_id), "$[0].value")==1).all()
            # query = self.session().query(Event).filter(func.json_contains(Event.attributes, json.dumps(account_id), "$[0].value")==1).all()
            # query = Query(Event).filter(Event.attributes.comparator.contains([account_id]))
            resultproxy = self.session.execute("SELECT * FROM %s.data_event WHERE module_id='balances' AND event_id='Transfer' AND JSON_CONTAINS(attributes->'$[*].value', json_array('%s')) ORDER BY block_id DESC"% (settings.DB_NAME, account_id))
            event_results = [{column: value for column, value in rowproxy.items()} for rowproxy in resultproxy]
            events = []
            for r in event_results:
                events.append(
                    Event(
                        block_id=r['block_id'],
                        event_idx=r['event_idx'],
                        phase=r['phase'],
                        extrinsic_idx=r['extrinsic_idx'],
                        type=r['type'],
                        spec_version_id=r['spec_version_id'],
                        module_id=r['module_id'],
                        event_id=r['event_id'],
                        system=r['system'],
                        module=r['module'] ,
                        attributes=json.loads(r['attributes']),
                        codec_error=r['codec_error']
                    )
                )
            for i in events:
                sender = bytearray.fromhex(i.attributes[0]['value'].replace('0x','')).decode().rstrip(' \t\r\n\0')
                reciever = bytearray.fromhex(i.attributes[1]['value'].replace('0x','')).decode().rstrip(' \t\r\n\0')
                
                if not auth_user or auth_user != did:
                    print('User unauthenticated')
                    sender = sender[:settings.STR_MASK_LEN].ljust(settings.STR_DID_LEN, "*")
                    reciever = reciever[:settings.STR_MASK_LEN].ljust(settings.STR_DID_LEN, "*")
                
                sender_data = {
                    'type': 'account',
                    'id': i.attributes[0]['value'].replace('0x', ''),
                    'attributes': {
                        'id': i.attributes[0]['value'].replace('0x', ''),
                        'address': sender
                        # 'address': s[:settings.STR_MASK_LEN].ljust(settings.STR_DID_LEN, "*")
                        # 'address': ss58_encode(item.attributes[0]['value'].replace('0x', ''), settings.SUBSTRATE_ADDRESS_TYPE)
                    }
                }
                destination_data = {
                    'type': 'account',
                    'id': i.attributes[1]['value'].replace('0x', ''),
                    'attributes': {
                        'id': i.attributes[1]['value'].replace('0x', ''),
                        'address': reciever
                        # 'address': s[:settings.STR_MASK_LEN].ljust(settings.STR_DID_LEN, "*")
                        # 'address': ss58_encode(item.attributes[1]['value'].replace('0x', ''), settings.SUBSTRATE_ADDRESS_TYPE)
                    }
                }

                # Some networks don't have fees
                if len(i.attributes) == 4:
                    fee = i.attributes[3]['value']
                else:
                    fee = 0
                block = Block.query(self.session).get(i.block_id)
                transfer_data.append({
                    'type': 'balancetransfer',
                    'id': '{}-{}'.format(i.block_id, i.extrinsic_idx),
                    'attributes': {
                        'block_id': i.block_id,
                        'datetime': block.datetime.replace(tzinfo=pytz.UTC).isoformat(),
                        'event_idx': '{}-{}'.format(i.block_id, i.extrinsic_idx),
                        'sender': sender_data,
                        'destination': destination_data,
                        'value': i.attributes[2]['value'],
                        'fee': fee
                    }
                })
            response = self.get_jsonapi_response(
                    data=transfer_data
                )
            # hack for handling list of events 
            # print("returning resp", json.dumps(transfer_data))
            resp.body=json.dumps({
                "data": transfer_data
            })        
        else:
            resp.status = falcon.HTTP_400 
            resp.body=json.dumps({
                "data": [],
                "error": "ParamterException: Required DID"
            })       
           
class TopHoldersListResource(JSONAPIListResource):
    def get_query(self):
        transfer_data = [] 
        query = """SELECT tt.block_id, tt.account_id, tt.balance_total, tt.balance_free, tt.balance_reserved
                                        FROM metascan.data_account_info_snapshot tt
                                            INNER JOIN
                                                (SELECT account_id, MAX(block_id) AS MaxBlockId
                                                FROM metascan.data_account_info_snapshot
                                                GROUP BY account_id) groupedtt 
                                            ON tt.account_id = groupedtt.account_id 
                                            AND tt.block_id = groupedtt.MaxBlockId WHERE tt.account_id LIKE "6469643a737369643a%" ORDER BY tt.balance_total Desc LIMIT 100"""
        
            
        resultproxy = self.session.execute(query)
        results = [{column: value for column, value in rowproxy.items()} for rowproxy in resultproxy]
        # sender = bytearray.fromhex(i.attributes[0]['value'].replace('0x','')).decode().rstrip(' \t\r\n\0')
            
        print(results)  
        return results
        
    def serialize_item(self, item):       
        did = bytearray.fromhex(item['account_id'].replace('0x','')).decode().rstrip(' \t\r\n\0')
        highest_balance = getHighestFormBalance(item['balance_total'])
        return {
            "block_id": item['block_id'],
            "did": did,
            "balance_total": str(highest_balance),
            "balance_free": str(getHighestFormBalance(item['balance_free'])),
            "balance_reserved": str(getHighestFormBalance(item['balance_reserved'])),
            "percentage": str(getPercentageBalance(highest_balance))
        }

def getHighestFormBalance(balanceInDecimal):
    return 0 if (balanceInDecimal == 0 or balanceInDecimal == None) else round((balanceInDecimal / 1000000), 6)

def getPercentageBalance(balanceInDecimal):
    percentage_factor = decimal.Decimal("100")
    return 0 if (balanceInDecimal == 0 or balanceInDecimal == None) else round(percentage_factor *(balanceInDecimal / METAMUI_TOTAL), 2)

class BalanceTransferListResource(JSONAPIListResource):

    def get_query(self):
        return Event.query(self.session).filter(
            Event.module_id == 'balances', Event.event_id == 'Transfer'
        ).order_by(Event.block_id.desc())

    def apply_filters(self, query, params):
        if params.get('filter[address]'):
            print('BalanceTransferListResource: ', params.get('filter[address]'))
            # Since we are storing balance in DID, we need to parse hex to did
            account_id = bytearray.fromhex(params.get('filter[address]').replace('0x','')).decode()
            # if len(params.get('filter[address]')) == 64:
            #     account_id = params.get('filter[address]')
            # else:
            #     try:
            #         account_id = ss58_decode(params.get('filter[address]'), settings.SUBSTRATE_ADDRESS_TYPE)
            #     except ValueError:
            #         return query.filter(False)

            search_index = SearchIndex.query(self.session).filter(
                SearchIndex.index_type_id.in_([
                    settings.SEARCH_INDEX_BALANCETRANSFER,
                    settings.SEARCH_INDEX_CLAIMS_CLAIMED,
                    settings.SEARCH_INDEX_BALANCES_DEPOSIT,
                    settings.SEARCH_INDEX_STAKING_REWARD
                ]),
                SearchIndex.account_id == account_id
            ).order_by(SearchIndex.sorting_value.desc())

            query = Event.query(self.session).filter(tuple_(Event.block_id, Event.event_idx).in_(
                [[s.block_id, s.event_idx] for s in search_index]
            )).order_by(Event.block_id.desc())


        return query

    def serialize_item(self, item):

        if item.event_id == 'Transfer':

            # sender = Account.query(self.session).get(item.attributes[0]['value'].replace('0x', ''))

            # if sender:
            #     sender_data = sender.serialize()
            # else:
            s = bytearray.fromhex(item.attributes[0]['value'].replace('0x','')).decode().rstrip(' \t\r\n\0')
            sender_data = {
                'type': 'account',
                'id': item.attributes[0]['value'].replace('0x', ''),
                'attributes': {
                    'id': item.attributes[0]['value'].replace('0x', ''),
                    'address': s[:settings.STR_MASK_LEN].ljust(settings.STR_DID_LEN, "*")
                    # 'address': ss58_encode(item.attributes[0]['value'].replace('0x', ''), settings.SUBSTRATE_ADDRESS_TYPE)
                }
            }

            # destination = Account.query(self.session).get(item.attributes[1]['value'].replace('0x', ''))

            # if destination:
            #     destination_data = destination.serialize()
            # else:
            s = bytearray.fromhex(item.attributes[1]['value'].replace('0x','')).decode().rstrip(' \t\r\n\0')
            destination_data = {
                'type': 'account',
                'id': item.attributes[1]['value'].replace('0x', ''),
                'attributes': {
                    'id': item.attributes[1]['value'].replace('0x', ''),
                    'address': s[:settings.STR_MASK_LEN].ljust(settings.STR_DID_LEN, "*")
                    # 'address': ss58_encode(item.attributes[1]['value'].replace('0x', ''), settings.SUBSTRATE_ADDRESS_TYPE)
                }
            }
            # Some networks don't have fees
            if len(item.attributes) == 4:
                fee = item.attributes[3]['value']
            else:
                fee = 0

            value = item.attributes[2]['value']
        elif item.event_id == 'Claimed':

            fee = 0
            sender_data = {'name': 'Claim', 'eth_address': item.attributes[1]['value']}
            destination_data = {}
            value = item.attributes[2]['value']

        elif item.event_id == 'Deposit':

            fee = 0
            sender_data = {'name': 'Deposit'}
            destination_data = {}
            value = item.attributes[1]['value']

        elif item.event_id == 'Reward':
            fee = 0
            sender_data = {'name': 'Staking reward'}
            destination_data = {}
            value = item.attributes[1]['value']
        else:
            sender_data = {}
            fee = 0
            destination_data = {}
            value = None

        return {
            'type': 'balancetransfer',
            'id': '{}-{}'.format(item.block_id, item.extrinsic_idx),
            'attributes': {
                'block_id': item.block_id,
                'event_id': item.event_id,
                'event_idx': '{}-{}'.format(item.block_id, item.extrinsic_idx),
                'sender': sender_data,
                'destination': destination_data,
                'value': value,
                'fee': fee
            }
        }


class BalanceTransferDetailResource(JSONAPIDetailResource):

    def get_item(self, item_id):
        return Event.query(self.session).get(item_id.split('-'))

    def serialize_item(self, item, auth_user=False):

        # sender = Account.query(self.session).get(item.attributes[0]['value'].replace('0x', ''))

        # if sender:
        #     sender_data = sender.serialize()
        # else:
        # TODO: Remove the hex did in id by removing its dependancies in explorer/ Metawallet App 
        sender = bytearray.fromhex(item.attributes[0]['value'].replace('0x','')).decode().rstrip(' \t\r\n\0')
        receiver = bytearray.fromhex(item.attributes[1]['value'].replace('0x','')).decode().rstrip(' \t\r\n\0')
        if not auth_user or auth_user not in [sender, receiver]:
            print('User not authenticated')
            sender = sender[:settings.STR_MASK_LEN].ljust(settings.STR_DID_LEN, "*")
            receiver = receiver[:settings.STR_MASK_LEN].ljust(settings.STR_DID_LEN, "*")
        sender_data = {
            'type': 'account',
            'id': item.attributes[0]['value'].replace('0x', ''),
            'attributes': {
                'id': item.attributes[0]['value'].replace('0x', ''),
                'address': sender
                # 'address': s[:settings.STR_MASK_LEN].ljust(settings.STR_DID_LEN, "*")
                # 'address': ss58_encode(item.attributes[0]['value'].replace('0x', ''), settings.SUBSTRATE_ADDRESS_TYPE)
            }
        }

        # destination = Account.query(self.session).get(item.attributes[1]['value'].replace('0x', ''))

        # if destination:
        #     destination_data = destination.serialize()
        # else:
        destination_data = {
            'type': 'account',
            'id': item.attributes[1]['value'].replace('0x', ''),
            'attributes': {
                'id': item.attributes[1]['value'].replace('0x', ''),
                'address': receiver
                # 'address': s[:settings.STR_MASK_LEN].ljust(settings.STR_DID_LEN, "*")
                # 'address': ss58_encode(item.attributes[1]['value'].replace('0x', ''), settings.SUBSTRATE_ADDRESS_TYPE)
            }
        }

        # Some networks don't have fees
        if len(item.attributes) == 4:
            fee = item.attributes[3]['value']
        else:
            fee = 0

        return {
            'type': 'balancetransfer',
            'id': '{}-{}'.format(item.block_id, item.extrinsic_idx),
            'attributes': {
                'block_id': item.block_id,
                'event_idx': '{}-{}'.format(item.block_id, item.extrinsic_idx),
                'sender': sender_data,
                'destination': destination_data,
                'value': item.attributes[2]['value'],
                'fee': fee
            }
        }


class AccountResource(JSONAPIListResource):

    def get_query(self):
        return Account.query(self.session).order_by(
            Account.balance_total.desc()
        )

    def apply_filters(self, query, params):

        if params.get('filter[is_validator]'):
            query = query.filter_by(is_validator=True)

        if params.get('filter[is_nominator]'):
            query = query.filter_by(is_nominator=True)

        if params.get('filter[is_council_member]'):
            query = query.filter_by(is_council_member=True)

        if params.get('filter[is_registrar]'):
            query = query.filter_by(is_registrar=True)

        if params.get('filter[is_sudo]'):
            query = query.filter_by(is_sudo=True)

        if params.get('filter[is_tech_comm_member]'):
            query = query.filter_by(is_tech_comm_member=True)

        if params.get('filter[is_treasury]'):
            query = query.filter_by(is_treasury=True)

        if params.get('filter[was_validator]'):
            query = query.filter_by(was_validator=True)

        if params.get('filter[was_nominator]'):
            query = query.filter_by(was_nominator=True)

        if params.get('filter[was_council_member]'):
            query = query.filter_by(was_council_member=True)

        if params.get('filter[was_registrar]'):
            query = query.filter_by(was_registrar=True)

        if params.get('filter[was_sudo]'):
            query = query.filter_by(was_sudo=True)

        if params.get('filter[was_tech_comm_member]'):
            query = query.filter_by(was_tech_comm_member=True)

        if params.get('filter[has_identity]'):
            query = query.filter_by(has_identity=True, identity_judgement_bad=0)

        if params.get('filter[has_subidentity]'):
            query = query.filter_by(has_subidentity=True, identity_judgement_bad=0)

        if params.get('filter[identity_judgement_good]'):
            query = query.filter(Account.identity_judgement_good > 0, Account.identity_judgement_bad == 0)

        if params.get('filter[blacklist]'):
            query = query.filter(Account.identity_judgement_bad > 0)

        return query


class AccountDetailResource(JSONAPIDetailResource):

    cache_expiration_time = 12

    def __init__(self):
        RuntimeConfiguration().update_type_registry(load_type_registry_preset('default'))
        if settings.TYPE_REGISTRY != 'default':
            RuntimeConfiguration().update_type_registry(load_type_registry_preset(settings.TYPE_REGISTRY))
        super(AccountDetailResource, self).__init__()

    def get_item(self, item_id):
        return Account.query(self.session).filter(or_(Account.address == item_id, Account.index_address == item_id)).first()

    def get_relationships(self, include_list, item):
        relationships = {}

        if 'recent_extrinsics' in include_list:
            relationships['recent_extrinsics'] = Extrinsic.query(self.session).filter_by(
                address=item.id).order_by(Extrinsic.block_id.desc())[:10]

        if 'indices' in include_list:
            relationships['indices'] = AccountIndex.query(self.session).filter_by(
                account_id=item.id).order_by(AccountIndex.updated_at_block.desc())

        return relationships

    def serialize_item(self, item, auth_user=False):
        data = item.serialize()

        # Get balance history
        account_info_snapshot = AccountInfoSnapshot.query(self.session).filter_by(
                account_id=item.id
        ).order_by(AccountInfoSnapshot.block_id.desc())[:1000]

        data['attributes']['balance_history'] = [
            {
                'name': "Total balance",
                'type': 'line',
                'data': [
                    [item.block_id, float((item.balance_total or 0) / 10**settings.SUBSTRATE_TOKEN_DECIMALS)]
                    for item in reversed(account_info_snapshot)
                ],
            }
        ]

        if settings.USE_NODE_RETRIEVE_BALANCES == 'True':

            substrate = SubstrateInterface(settings.SUBSTRATE_RPC_URL)

            # if settings.SUBSTRATE_STORAGE_BALANCE == 'Account':
            #     storage_call = RuntimeStorage.query(self.session).filter_by(
            #         module_id='system',
            #         name='Account',
            #     ).order_by(RuntimeStorage.spec_version.desc()).first()

            #     if storage_call:
            #         account_data = substrate.get_storage(
            #             block_hash=None,
            #             module='System',
            #             function='Account',
            #             params=item.id,
            #             return_scale_type=storage_call.type_value,
            #             hasher=storage_call.type_hasher,
            #             metadata_version=settings.SUBSTRATE_METADATA_VERSION
            #         )

            #         if account_data:
            #             data['attributes']['free_balance'] = account_data['data']['free']
            #             data['attributes']['reserved_balance'] = account_data['data']['reserved']
            #             data['attributes']['misc_frozen_balance'] = account_data['data']['miscFrozen']
            #             data['attributes']['fee_frozen_balance'] = account_data['data']['feeFrozen']
            #             data['attributes']['nonce'] = account_data['nonce']

            # elif settings.SUBSTRATE_STORAGE_BALANCE == 'Balances.Account':

            #     storage_call = RuntimeStorage.query(self.session).filter_by(
            #         module_id='balances',
            #         name='Account',
            #     ).order_by(RuntimeStorage.spec_version.desc()).first()

            #     if storage_call:
            #         account_data = substrate.get_storage(
            #             block_hash=None,
            #             module='Balances',
            #             function='Account',
            #             params=item.id,
            #             return_scale_type=storage_call.type_value,
            #             hasher=storage_call.type_hasher,
            #             metadata_version=settings.SUBSTRATE_METADATA_VERSION
            #         )

            #         if account_data:
            #             data['attributes']['balance_free'] = account_data['free']
            #             data['attributes']['balance_reserved'] = account_data['reserved']
            #             data['attributes']['misc_frozen_balance'] = account_data['miscFrozen']
            #             data['attributes']['fee_frozen_balance'] = account_data['feeFrozen']
            #             data['attributes']['nonce'] = None
            # elif settings.SUBSTRATE_STORAGE_BALANCE == 'Did.Account':
            print('Did account type')
            storage_call = RuntimeStorage.query(self.session).filter_by(
                module_id='did',
                name='Account',
            ).order_by(RuntimeStorage.spec_version.desc()).first()

            if storage_call:
                account_data = substrate.get_storage(
                    block_hash=None,
                    module='Did',
                    function='Account',
                    params=item.id,
                    return_scale_type=storage_call.type_value,
                    hasher=storage_call.type_hasher,
                    metadata_version=settings.SUBSTRATE_METADATA_VERSION
                )

                if account_data:
                    data['attributes']['balance_free'] = account_data['free']
                    data['attributes']['balance_reserved'] = account_data['reserved']
                    data['attributes']['misc_frozen_balance'] = account_data['miscFrozen']
                    data['attributes']['fee_frozen_balance'] = account_data['feeFrozen']
                    data['attributes']['nonce'] = None
            # else:

            #     storage_call = RuntimeStorage.query(self.session).filter_by(
            #         module_id='balances',
            #         name='FreeBalance',
            #     ).order_by(RuntimeStorage.spec_version.desc()).first()

            #     if storage_call:
            #         data['attributes']['free_balance'] = substrate.get_storage(
            #             block_hash=None,
            #             module='Balances',
            #             function='FreeBalance',
            #             params=item.id,
            #             return_scale_type=storage_call.type_value,
            #             hasher=storage_call.type_hasher,
            #             metadata_version=settings.SUBSTRATE_METADATA_VERSION
            #         )

            #     storage_call = RuntimeStorage.query(self.session).filter_by(
            #         module_id='balances',
            #         name='ReservedBalance',
            #     ).order_by(RuntimeStorage.spec_version.desc()).first()

            #     if storage_call:
            #         data['attributes']['reserved_balance'] = substrate.get_storage(
            #             block_hash=None,
            #             module='Balances',
            #             function='ReservedBalance',
            #             params=item.id,
            #             return_scale_type=storage_call.type_value,
            #             hasher=storage_call.type_hasher,
            #             metadata_version=settings.SUBSTRATE_METADATA_VERSION
            #         )

            #     storage_call = RuntimeStorage.query(self.session).filter_by(
            #         module_id='system',
            #         name='AccountNonce',
            #     ).order_by(RuntimeStorage.spec_version.desc()).first()

            #     if storage_call:

            #         data['attributes']['nonce'] = substrate.get_storage(
            #             block_hash=None,
            #             module='System',
            #             function='AccountNonce',
            #             params=item.id,
            #             return_scale_type=storage_call.type_value,
            #             hasher=storage_call.type_hasher,
            #             metadata_version=settings.SUBSTRATE_METADATA_VERSION
            #         )

        return data


class AccountIndexListResource(JSONAPIListResource):

    def get_query(self):
        return AccountIndex.query(self.session).order_by(
            AccountIndex.updated_at_block.desc()
        )


class AccountIndexDetailResource(JSONAPIDetailResource):

    def get_item(self, item_id):
        return AccountIndex.query(self.session).filter_by(short_address=item_id).first()

    def get_relationships(self, include_list, item):
        relationships = {}

        if 'recent_extrinsics' in include_list:
            relationships['recent_extrinsics'] = Extrinsic.query(self.session).filter_by(
                address=item.account_id).order_by(Extrinsic.block_id.desc())[:10]

        return relationships

    def serialize_item(self, item, auth_user=False):
        data = item.serialize()

        if item.account:
            data['attributes']['account'] = item.account.serialize()

        return data


class SessionListResource(JSONAPIListResource):

    cache_expiration_time = 60

    def get_query(self):
        return Session.query(self.session).order_by(
            Session.id.desc()
        )


class SessionDetailResource(JSONAPIDetailResource):

    def get_item(self, item_id):
        return Session.query(self.session).get(item_id)

    def get_relationships(self, include_list, item):
        relationships = {}

        if 'blocks' in include_list:
            relationships['blocks'] = Block.query(self.session).filter_by(
                session_id=item.id
            ).order_by(Block.id.desc())

        if 'validators' in include_list:
            relationships['validators'] = SessionValidator.query(self.session).filter_by(
                session_id=item.id
            ).order_by(SessionValidator.rank_validator)

        return relationships


class SessionValidatorListResource(JSONAPIListResource):

    cache_expiration_time = 60

    def get_query(self):
        return SessionValidator.query(self.session).order_by(
            SessionValidator.session_id, SessionValidator.rank_validator
        )

    def apply_filters(self, query, params):

        if params.get('filter[latestSession]'):

            session = Session.query(self.session).order_by(Session.id.desc()).first()

            query = query.filter_by(session_id=session.id)

        return query


class SessionValidatorDetailResource(JSONAPIDetailResource):

    def get_item(self, item_id):

        if len(item_id.split('-')) != 2:
            return None

        session_id, rank_validator = item_id.split('-')
        return SessionValidator.query(self.session).filter_by(
            session_id=session_id,
            rank_validator=rank_validator
        ).first()

    def get_relationships(self, include_list, item):
        relationships = {}

        if 'nominators' in include_list:
            relationships['nominators'] = SessionNominator.query(self.session).filter_by(
                session_id=item.session_id, rank_validator=item.rank_validator
            ).order_by(SessionNominator.rank_nominator)

        return relationships

    def serialize_item(self, item, auth_user=False):
        data = item.serialize()

        if item.validator_stash_account:
            data['attributes']['validator_stash_account'] = item.validator_stash_account.serialize()

        if item.validator_controller_account:
            data['attributes']['validator_controller_account'] = item.validator_controller_account.serialize()

        return data


class SessionNominatorListResource(JSONAPIListResource):

    cache_expiration_time = 60

    def get_query(self):
        return SessionNominator.query(self.session).order_by(
            SessionNominator.session_id, SessionNominator.rank_validator, SessionNominator.rank_nominator
        )

    def apply_filters(self, query, params):

        if params.get('filter[latestSession]'):

            session = Session.query(self.session).order_by(Session.id.desc()).first()

            query = query.filter_by(session_id=session.id)

        return query


class ContractListResource(JSONAPIListResource):

    def get_query(self):
        return Contract.query(self.session).order_by(
            Contract.created_at_block.desc()
        )


class ContractDetailResource(JSONAPIDetailResource):

    def get_item(self, item_id):
        return Contract.query(self.session).get(item_id)


class RuntimeListResource(JSONAPIListResource):

    cache_expiration_time = 60

    def get_query(self):
        return Runtime.query(self.session).order_by(
            Runtime.id.desc()
        )


class RuntimeDetailResource(JSONAPIDetailResource):

    def get_item(self, item_id):
        return Runtime.query(self.session).get(item_id)

    def get_relationships(self, include_list, item):
        relationships = {}

        if 'modules' in include_list:
            relationships['modules'] = RuntimeModule.query(self.session).filter_by(
                spec_version=item.spec_version
            ).order_by('lookup', 'id')

        if 'types' in include_list:
            relationships['types'] = RuntimeType.query(self.session).filter_by(
                spec_version=item.spec_version
            ).order_by('type_string')

        return relationships


class RuntimeCallListResource(JSONAPIListResource):

    cache_expiration_time = 3600

    def apply_filters(self, query, params):

        if params.get('filter[latestRuntime]'):

            latest_runtime = Runtime.query(self.session).order_by(Runtime.spec_version.desc()).first()

            query = query.filter_by(spec_version=latest_runtime.spec_version)

        if params.get('filter[module_id]'):

            query = query.filter_by(module_id=params.get('filter[module_id]'))

        return query

    def get_query(self):
        return RuntimeCall.query(self.session).order_by(
            RuntimeCall.spec_version.asc(), RuntimeCall.module_id.asc(), RuntimeCall.call_id.asc()
        )


class RuntimeCallDetailResource(JSONAPIDetailResource):

    def get_item_url_name(self):
        return 'runtime_call_id'

    def get_item(self, item_id):

        if len(item_id.split('-')) != 3:
            return None

        spec_version, module_id, call_id = item_id.split('-')
        return RuntimeCall.query(self.session).filter_by(
            spec_version=spec_version,
            module_id=module_id,
            call_id=call_id
        ).first()

    def get_relationships(self, include_list, item):
        relationships = {}

        if 'params' in include_list:
            relationships['params'] = RuntimeCallParam.query(self.session).filter_by(
                runtime_call_id=item.id).order_by('id')

        if 'recent_extrinsics' in include_list:
            relationships['recent_extrinsics'] = Extrinsic.query(self.session).filter_by(
                call_id=item.call_id, module_id=item.module_id).order_by(Extrinsic.block_id.desc())[:10]

        return relationships


class RuntimeEventListResource(JSONAPIListResource):

    cache_expiration_time = 3600

    def apply_filters(self, query, params):

        if params.get('filter[latestRuntime]'):

            latest_runtime = Runtime.query(self.session).order_by(Runtime.spec_version.desc()).first()

            query = query.filter_by(spec_version=latest_runtime.spec_version)

        if params.get('filter[module_id]'):

            query = query.filter_by(module_id=params.get('filter[module_id]'))

        return query

    def get_query(self):
        return RuntimeEvent.query(self.session).order_by(
            RuntimeEvent.spec_version.asc(), RuntimeEvent.module_id.asc(), RuntimeEvent.event_id.asc()
        )


class RuntimeEventDetailResource(JSONAPIDetailResource):

    def get_item_url_name(self):
        return 'runtime_event_id'

    def get_item(self, item_id):

        if len(item_id.split('-')) != 3:
            return None

        spec_version, module_id, event_id = item_id.split('-')
        return RuntimeEvent.query(self.session).filter_by(
            spec_version=spec_version,
            module_id=module_id,
            event_id=event_id
        ).first()

    def get_relationships(self, include_list, item):
        relationships = {}

        if 'attributes' in include_list:
            relationships['attributes'] = RuntimeEventAttribute.query(self.session).filter_by(
                runtime_event_id=item.id).order_by('id')

        if 'recent_events' in include_list:
            relationships['recent_events'] = Event.query(self.session).filter_by(
                event_id=item.event_id, module_id=item.module_id).order_by(Event.block_id.desc())[:10]

        return relationships


class RuntimeTypeListResource(JSONAPIListResource):

    cache_expiration_time = 3600

    def get_query(self):
        return RuntimeType.query(self.session).order_by(
            'spec_version', 'type_string'
        )

    def apply_filters(self, query, params):

        if params.get('filter[latestRuntime]'):

            latest_runtime = Runtime.query(self.session).order_by(Runtime.spec_version.desc()).first()

            query = query.filter_by(spec_version=latest_runtime.spec_version)

        return query


class RuntimeModuleListResource(JSONAPIListResource):

    cache_expiration_time = 3600

    def get_query(self):
        return RuntimeModule.query(self.session).order_by(
            'spec_version', 'name'
        )

    def apply_filters(self, query, params):

        if params.get('filter[latestRuntime]'):

            latest_runtime = Runtime.query(self.session).order_by(Runtime.spec_version.desc()).first()

            query = query.filter_by(spec_version=latest_runtime.spec_version)

        return query


class RuntimeModuleDetailResource(JSONAPIDetailResource):

    def get_item(self, item_id):

        if len(item_id.split('-')) != 2:
            return None

        spec_version, module_id = item_id.split('-')
        return RuntimeModule.query(self.session).filter_by(spec_version=spec_version, module_id=module_id).first()

    def get_relationships(self, include_list, item):
        relationships = {}

        if 'calls' in include_list:
            relationships['calls'] = RuntimeCall.query(self.session).filter_by(
                spec_version=item.spec_version, module_id=item.module_id).order_by(
                'lookup', 'id')

        if 'events' in include_list:
            relationships['events'] = RuntimeEvent.query(self.session).filter_by(
                spec_version=item.spec_version, module_id=item.module_id).order_by(
                'lookup', 'id')

        if 'storage' in include_list:
            relationships['storage'] = RuntimeStorage.query(self.session).filter_by(
                spec_version=item.spec_version, module_id=item.module_id).order_by(
                'name')

        if 'constants' in include_list:
            relationships['constants'] = RuntimeConstant.query(self.session).filter_by(
                spec_version=item.spec_version, module_id=item.module_id).order_by(
                'name')

        if 'errors' in include_list:
            relationships['errors'] = RuntimeErrorMessage.query(self.session).filter_by(
                spec_version=item.spec_version, module_id=item.module_id).order_by(
                'name').order_by(RuntimeErrorMessage.index)

        return relationships


class RuntimeStorageDetailResource(JSONAPIDetailResource):

    def get_item(self, item_id):

        if len(item_id.split('-')) != 3:
            return None

        spec_version, module_id, name = item_id.split('-')
        return RuntimeStorage.query(self.session).filter_by(
            spec_version=spec_version,
            module_id=module_id,
            name=name
        ).first()


class RuntimeConstantListResource(JSONAPIListResource):

    cache_expiration_time = 3600

    def get_query(self):
        return RuntimeConstant.query(self.session).order_by(
            RuntimeConstant.spec_version.desc(), RuntimeConstant.module_id.asc(), RuntimeConstant.name.asc()
        )


class RuntimeConstantDetailResource(JSONAPIDetailResource):

    def get_item(self, item_id):

        if len(item_id.split('-')) != 3:
            return None

        spec_version, module_id, name = item_id.split('-')
        return RuntimeConstant.query(self.session).filter_by(
            spec_version=spec_version,
            module_id=module_id,
            name=name
        ).first()
