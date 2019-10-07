import struct
import logging
import asyncio as aio
from typing import Optional, Any, Awaitable, Coroutine, Tuple, List
from .utils import gen_nonce
from .encoding import BinaryStr, TypeNumber, LpTypeNumber, parse_interest, \
    parse_network_nack, parse_data, DecodeError, Name, NonStrictName, MetaInfo, \
    make_data, InterestParam, make_interest, FormalName, SignaturePtrs
from .security.digest_keychain import make_digest_keychain
from .security.digest_validator import sha256_digest_checker, params_sha256_checker
from .transport.stream_socket import Face, UnixFace
from .app_support.nfd_mgmt import make_reg_route_cmd
from .name_tree import NameTrie, InterestTreeNode, PrefixTreeNode
from .types import NetworkError, InterestTimeout, Validator, KeyChain, Route, InterestCanceled, \
    InterestNack, ValidationFailure


class NDNApp:
    face: Face = None
    keychain: KeyChain = None
    _int_tree: NameTrie = None
    _prefix_tree: NameTrie = None
    int_validator: Validator = None
    data_validator: Validator = None
    _autoreg_routes: List[Tuple[FormalName, Route, Optional[Validator]]]

    def __init__(self):
        self.face = UnixFace(self._receive)
        self.keychain = make_digest_keychain()
        self._int_tree = NameTrie()
        self._prefix_tree = NameTrie()
        self.data_validator = sha256_digest_checker
        self.int_validator = sha256_digest_checker
        self._autoreg_routes = []

    async def _receive(self, typ: int, data: BinaryStr):
        logging.debug('Packet received %s, %s' % (typ, bytes(data)))
        try:
            if typ == TypeNumber.INTEREST:
                try:
                    name, param, app_param, sig = parse_interest(data, with_tl=False)
                except (DecodeError, TypeError, ValueError, struct.error):
                    raise DecodeError
                logging.debug('Interest received %s' % Name.to_str(name))
                await self._on_interest(name, param, app_param, sig)
            elif typ == TypeNumber.DATA:
                try:
                    name, meta_info, content, sig = parse_data(data, with_tl=False)
                except (DecodeError, TypeError, ValueError, struct.error):
                    raise DecodeError
                logging.debug('Data received %s' % Name.to_str(name))
                await self._on_data(name, meta_info, content, sig)
            elif typ == LpTypeNumber.LP_PACKET:
                try:
                    nack_reason, interest = parse_network_nack(data, with_tl=False)
                    name, _, _, _ = parse_interest(interest, with_tl=True)
                except (DecodeError, TypeError, ValueError, struct.error):
                    raise DecodeError
                logging.debug('NetworkNack received %s, reason=%s' % (Name.to_str(name), nack_reason))
                self._on_nack(name, nack_reason)
        except DecodeError:
            logging.warning('Unable to decode received packet')

    def put_raw_packet(self, data: BinaryStr):
        if not self.face.running:
            raise NetworkError('cannot send packet before connected')
        self.face.send(data)

    def prepare_data(self, name: NonStrictName, content: Optional[BinaryStr] = None, **kwargs):
        signer = self.keychain(kwargs)
        if 'meta_info' in kwargs:
            meta_info = kwargs['meta_info']
        else:
            meta_info = MetaInfo.from_dict(kwargs)
        return make_data(name, meta_info, content, signer=signer, **kwargs)

    def put_data(self, name: NonStrictName, content: Optional[BinaryStr] = None, **kwargs):
        self.put_raw_packet(self.prepare_data(name, content, **kwargs))

    def express_interest(self,
                         name: NonStrictName,
                         app_param: Optional[BinaryStr] = None,
                         validator: Optional[Validator] = None,
                         **kwargs) -> Coroutine[Any, None, Tuple[FormalName, MetaInfo, Optional[BinaryStr]]]:
        if not self.face.running:
            raise NetworkError('cannot send packet before connected')
        if app_param is not None:
            signer = self.keychain(kwargs)
        else:
            signer = None
        if 'interest_param' in kwargs:
            interest_param = kwargs['interest_param']
        else:
            if 'nonce' not in kwargs:
                kwargs['nonce'] = gen_nonce()
            interest_param = InterestParam.from_dict(kwargs)
        interest, final_name = make_interest(name, interest_param, app_param,
                                             signer=signer, need_final_name=True, **kwargs)
        future = aio.get_event_loop().create_future()
        node = self._int_tree.setdefault(final_name, InterestTreeNode())
        node.append_interest(future, interest_param)
        node.validator = validator
        self.face.send(interest)
        return self._wait_for_data(future, interest_param.lifetime, final_name, node)

    async def _wait_for_data(self, future: aio.Future, lifetime: int, name: FormalName, node: InterestTreeNode):
        lifetime = 100 if lifetime is None else lifetime
        try:
            data = await aio.wait_for(future, timeout=lifetime/1000.0)
        except aio.TimeoutError:
            if node.timeout(future):
                del self._int_tree[name]
            raise InterestTimeout()
        except aio.CancelledError:
            raise InterestCanceled()
        return data

    async def main_loop(self, after_start: Awaitable = None):
        async def starting_task():
            for name, route, validator in self._autoreg_routes:
                await self.register(name, route, validator)
            if after_start:
                await after_start

        await self.face.open()
        task = aio.ensure_future(starting_task())
        logging.debug('Connected to NFD node, start running...')
        await self.face.run()
        self.face.shutdown()
        self._clean_up()
        await task

    def _clean_up(self):
        for node in self._int_tree.itervalues():
            node.cancel()
        self._prefix_tree.clear()
        self._int_tree.clear()

    def shutdown(self):
        logging.info('Manually shutdown')
        self.face.shutdown()

    def run_forever(self, after_start: Awaitable = None):
        task = self.main_loop(after_start)
        try:
            aio.get_event_loop().run_until_complete(task)
        except KeyboardInterrupt:
            logging.info('Receiving Ctrl+C, shutdown')
        finally:
            self.face.shutdown()
        logging.debug('Face is down now')

    def route(self, name: NonStrictName, validator: Optional[Validator] = None):
        name = Name.normalize(name)

        def decorator(func: Route):
            self._autoreg_routes.append((name, func, validator))
            if self.face.running:
                aio.ensure_future(self.register(name, func, validator))
            return func
        return decorator

    async def register(self, name: NonStrictName, func: Route, validator: Optional[Validator] = None):
        name = Name.normalize(name)
        node = self._prefix_tree.setdefault(name, PrefixTreeNode())
        if node.callback:
            raise ValueError(f'Duplicated registration: {Name.to_str(name)}')
        node.callback = func
        if validator:
            node.validator = validator
        try:
            _, _, reply = await self.express_interest(make_reg_route_cmd(name), lifetime=1000)
            print(bytes(reply))  # TODO
            return True
        except (InterestNack, InterestTimeout, InterestCanceled, ValidationFailure):
            return False

    async def unregister(self, name: NonStrictName):
        name = Name.normalize(name)
        del self._prefix_tree[name]
        pass

    def _on_nack(self, name: FormalName, nack_reason: int):
        node = self._int_tree[name]
        if node:
            if node.nack_interest(nack_reason):
                del self._int_tree[name]

    async def _on_data(self, name: FormalName, meta_info: MetaInfo,
                       content: Optional[BinaryStr], sig: SignaturePtrs):
        clean_list = []
        for prefix, node in self._int_tree.prefixes(name):
            validator = node.validator if node.validator else self.data_validator
            if await validator(name, sig):
                clean = node.satisfy(name, meta_info, content, prefix != name)
            else:
                clean = node.invalid(name, meta_info, content)
            if clean:
                clean_list.append(prefix)
        for prefix in clean_list:
            del self._int_tree[prefix]

    async def _on_interest(self, name: FormalName, param: InterestParam,
                           app_param: Optional[BinaryStr], sig: SignaturePtrs):
        trie_step = self._prefix_tree.longest_prefix(name)
        if not trie_step:
            logging.warning('No route: %s' % name)
            return
        node = trie_step.value
        if app_param is not None or sig.signature_info is not None:
            if not await params_sha256_checker(name, sig):
                logging.warning('Drop malformed Interest: %s' % name)
                return
        if sig.signature_info is not None:
            validator = node.validator if node.validator else self.int_validator
            valid = await validator(name, sig)
        else:
            valid = True
        if not valid:
            logging.warning('Drop unvalidated Interest: %s' % name)
            return
        node.callback(name, param, app_param)
