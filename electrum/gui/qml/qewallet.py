from typing import Optional, TYPE_CHECKING, Sequence, List, Union
import queue
import time

from PyQt5.QtCore import pyqtProperty, pyqtSignal, pyqtSlot, QObject, QUrl, QTimer

from electrum.i18n import _
from electrum.util import register_callback, Satoshis, format_time, parse_max_spend
from electrum.logging import get_logger
from electrum.wallet import Wallet, Abstract_Wallet
from electrum import bitcoin
from electrum.transaction import PartialTxOutput
from electrum.invoices import   (Invoice, InvoiceError, PR_TYPE_ONCHAIN, PR_TYPE_LN,
                                 PR_DEFAULT_EXPIRATION_WHEN_CREATING, PR_PAID, PR_UNPAID, PR_UNKNOWN, PR_EXPIRED, PR_UNCONFIRMED, PR_TYPE_ONCHAIN, PR_TYPE_LN)

from .qerequestlistmodel import QERequestListModel
from .qetransactionlistmodel import QETransactionListModel
from .qeaddresslistmodel import QEAddressListModel

class QEWallet(QObject):
    __instances = []

    # this factory method should be used to instantiate QEWallet
    # so we have only one QEWallet for each electrum.wallet
    @classmethod
    def getInstanceFor(cls, wallet):
        for i in cls.__instances:
            if i.wallet == wallet:
                return i
        i = QEWallet(wallet)
        cls.__instances.append(i)
        return i

    _logger = get_logger(__name__)

    # emitted when wallet wants to display a user notification
    # actual presentation should be handled on app or window level
    userNotify = pyqtSignal(object, object)

    # shared signal for many static wallet properties
    dataChanged = pyqtSignal()

    isUptodateChanged = pyqtSignal()
    requestStatusChanged = pyqtSignal([str,int], arguments=['key','status'])
    requestCreateSuccess = pyqtSignal()
    requestCreateError = pyqtSignal([str,str], arguments=['code','error'])

    _network_signal = pyqtSignal(str, object)

    def __init__(self, wallet, parent=None):
        super().__init__(parent)
        self.wallet = wallet

        self._historyModel = QETransactionListModel(wallet)
        self._addressModel = QEAddressListModel(wallet)
        self._requestModel = QERequestListModel(wallet)

        self._historyModel.init_model()
        self._requestModel.init_model()

        self.tx_notification_queue = queue.Queue()
        self.tx_notification_last_time = 0

        self.notification_timer = QTimer(self)
        self.notification_timer.setSingleShot(False)
        self.notification_timer.setInterval(500)  # msec
        self.notification_timer.timeout.connect(self.notify_transactions)

        self._network_signal.connect(self.on_network_qt)
        interests = ['wallet_updated', 'new_transaction', 'status', 'verified',
                     'on_history', 'channel', 'channels_updated', 'payment_failed',
                     'payment_succeeded', 'invoice_status', 'request_status']
        # To avoid leaking references to "self" that prevent the
        # window from being GC-ed when closed, callbacks should be
        # methods of this class only, and specifically not be
        # partials, lambdas or methods of subobjects.  Hence...
        register_callback(self.on_network, interests)

    @pyqtProperty(bool, notify=isUptodateChanged)
    def isUptodate(self):
        return self.wallet.is_up_to_date()

    def on_network(self, event, *args):
        # Handle in GUI thread (_network_signal -> on_network_qt)
        self._network_signal.emit(event, args)

    def on_network_qt(self, event, args=None):
        # note: we get events from all wallets! args are heterogenous so we can't
        # shortcut here
        if event == 'status':
            self.isUptodateChanged.emit()
        elif event == 'request_status':
            wallet, addr, c = args
            if wallet == self.wallet:
                self._logger.debug('request status %d for address %s' % (c, addr))
                self.requestStatusChanged.emit(addr, c)
        elif event == 'new_transaction':
            wallet, tx = args
            if wallet == self.wallet:
                self.add_tx_notification(tx)
                self._historyModel.init_model() # TODO: be less dramatic
        elif event == 'verified':
            wallet, txid, info = args
            if wallet == self.wallet:
                self._historyModel.update_tx(txid, info)
        elif event == 'wallet_updated':
            wallet, = args
            if wallet == self.wallet:
                self._logger.debug('wallet %s updated' % str(wallet))
                self.balanceChanged.emit()
        else:
            self._logger.debug('unhandled event: %s %s' % (event, str(args)))


    def add_tx_notification(self, tx):
        self._logger.debug('new transaction event')
        self.tx_notification_queue.put(tx)
        if not self.notification_timer.isActive():
            self._logger.debug('starting wallet notification timer')
            self.notification_timer.start()

    def notify_transactions(self):
        if self.tx_notification_queue.qsize() == 0:
            self._logger.debug('queue empty, stopping wallet notification timer')
            self.notification_timer.stop()
            return
        if not self.wallet.up_to_date:
            return  # no notifications while syncing
        now = time.time()
        rate_limit = 20  # seconds
        if self.tx_notification_last_time + rate_limit > now:
            return
        self.tx_notification_last_time = now
        self._logger.info("Notifying app about new transactions")
        txns = []
        while True:
            try:
                txns.append(self.tx_notification_queue.get_nowait())
            except queue.Empty:
                break

        config = self.wallet.config
        # Combine the transactions if there are at least three
        if len(txns) >= 3:
            total_amount = 0
            for tx in txns:
                tx_wallet_delta = self.wallet.get_wallet_delta(tx)
                if not tx_wallet_delta.is_relevant:
                    continue
                total_amount += tx_wallet_delta.delta
            self.userNotify.emit(self.wallet, _("{} new transactions: Total amount received in the new transactions {}").format(len(txns), config.format_amount_and_units(total_amount)))
        else:
            for tx in txns:
                tx_wallet_delta = self.wallet.get_wallet_delta(tx)
                if not tx_wallet_delta.is_relevant:
                    continue
                self.userNotify.emit(self.wallet,
                    _("New transaction: {}").format(config.format_amount_and_units(tx_wallet_delta.delta)))

    historyModelChanged = pyqtSignal()
    @pyqtProperty(QETransactionListModel, notify=historyModelChanged)
    def historyModel(self):
        return self._historyModel

    addressModelChanged = pyqtSignal()
    @pyqtProperty(QEAddressListModel, notify=addressModelChanged)
    def addressModel(self):
        return self._addressModel

    requestModelChanged = pyqtSignal()
    @pyqtProperty(QERequestListModel, notify=requestModelChanged)
    def requestModel(self):
        return self._requestModel

    nameChanged = pyqtSignal()
    @pyqtProperty('QString', notify=nameChanged)
    def name(self):
        return self.wallet.basename()

    @pyqtProperty('QString', notify=dataChanged)
    def txinType(self):
        return self.wallet.get_txin_type(self.wallet.dummy_address())

    @pyqtProperty(bool, notify=dataChanged)
    def isWatchOnly(self):
        return self.wallet.is_watching_only()

    @pyqtProperty(bool, notify=dataChanged)
    def isDeterministic(self):
        return self.wallet.is_deterministic()

    @pyqtProperty(bool, notify=dataChanged)
    def isEncrypted(self):
        return self.wallet.storage.is_encrypted()

    @pyqtProperty(bool, notify=dataChanged)
    def isHardware(self):
        return self.wallet.storage.is_encrypted_with_hw_device()

    @pyqtProperty('QString', notify=dataChanged)
    def derivationPath(self):
        keystores = self.wallet.get_keystores()
        if len(keystores) > 1:
            self._logger.debug('multiple keystores not supported yet')
        return keystores[0].get_derivation_prefix()

    balanceChanged = pyqtSignal()

    @pyqtProperty(int, notify=balanceChanged)
    def frozenBalance(self):
        return self.wallet.get_frozen_balance()

    @pyqtProperty(int, notify=balanceChanged)
    def unconfirmedBalance(self):
        return self.wallet.get_balance()[1]

    @pyqtProperty(int, notify=balanceChanged)
    def confirmedBalance(self):
        c, u, x = self.wallet.get_balance()
        self._logger.info('balance: ' + str(c) + ' ' + str(u) + ' ' + str(x) + ' ')

        return c+x

    @pyqtSlot('QString', int, int, bool)
    def send_onchain(self, address, amount, fee=None, rbf=False):
        self._logger.info('send_onchain: %s %d' % (address,amount))
        coins = self.wallet.get_spendable_coins(None)
        if not bitcoin.is_address(address):
            self._logger.warning('Invalid Bitcoin Address: ' + address)
            return False

        outputs = [PartialTxOutput.from_address_and_value(address, amount)]
        self._logger.info(str(outputs))
        output_values = [x.value for x in outputs]
        if any(parse_max_spend(outval) for outval in output_values):
            output_value = '!'
        else:
            output_value = sum(output_values)
        self._logger.info(str(output_value))
        # see qt/confirm_tx_dialog qt/main_window
        tx = self.wallet.make_unsigned_transaction(coins=coins,outputs=outputs, fee=None)
        self._logger.info(str(tx.to_json()))

        if len(tx.to_json()['outputs']) < 2:
            self._logger.info('no change output??? : %s' % str(tx.to_json()['outputs']))
            return

        use_rbf = bool(self.wallet.config.get('use_rbf', True))
        tx.set_rbf(use_rbf)

        def cb(result):
            self._logger.info('signing was succesful? %s' % str(result))
        tx = self.wallet.sign_transaction(tx, None)
        if not tx.is_complete():
            self._logger.info('tx not complete')
            return

        self.network = self.wallet.network # TODO not always defined?

        try:
            self._logger.info('running broadcast in thread')
            self.network.run_from_another_thread(self.network.broadcast_transaction(tx))
            self._logger.info('broadcast submit done')
        except TxBroadcastError as e:
            self._logger.info(e)
            return
        except BestEffortRequestFailed as e:
            self._logger.info(e)
            return

        return

    def create_bitcoin_request(self, amount: int, message: str, expiration: int, ignore_gap: bool) -> Optional[str]:
        addr = self.wallet.get_unused_address()
        if addr is None:
            if not self.wallet.is_deterministic():  # imported wallet
                # TODO implement
                return
                #msg = [
                    #_('No more addresses in your wallet.'), ' ',
                    #_('You are using a non-deterministic wallet, which cannot create new addresses.'), ' ',
                    #_('If you want to create new addresses, use a deterministic wallet instead.'), '\n\n',
                    #_('Creating a new payment request will reuse one of your addresses and overwrite an existing request. Continue anyway?'),
                   #]
                #if not self.question(''.join(msg)):
                    #return
                #addr = self.wallet.get_receiving_address()
            else:  # deterministic wallet
                if not ignore_gap:
                    self.requestCreateError.emit('gaplimit',_("Warning: The next address will not be recovered automatically if you restore your wallet from seed; you may need to add it manually.\n\nThis occurs because you have too many unused addresses in your wallet. To avoid this situation, use the existing addresses first.\n\nCreate anyway?"))
                    return
                addr = self.wallet.create_new_address(False)

        req = self.wallet.make_payment_request(addr, amount, message, expiration)
        try:
            self.wallet.add_payment_request(req)
        except Exception as e:
            self.logger.exception('Error adding payment request')
            self.requestCreateError.emit('fatal',_('Error adding payment request') + ':\n' + repr(e))
        else:
            # TODO: check this flow. Only if alias is defined in config. OpenAlias?
            pass
            #self.sign_payment_request(addr)
        self._requestModel.add_request(req)
        return addr

    @pyqtSlot(int, 'QString', int)
    @pyqtSlot(int, 'QString', int, bool)
    @pyqtSlot(int, 'QString', int, bool, bool)
    def create_request(self, amount: int, message: str, expiration: int, is_lightning: bool = False, ignore_gap: bool = False):
        expiry = expiration #TODO: self.config.get('request_expiry', PR_DEFAULT_EXPIRATION_WHEN_CREATING)
        try:
            if is_lightning:
                if not self.wallet.lnworker.channels:
                    self.requestCreateError.emit('fatal',_("You need to open a Lightning channel first."))
                    return
                # TODO maybe show a warning if amount exceeds lnworker.num_sats_can_receive (as in kivy)
                key = self.wallet.lnworker.add_request(amount, message, expiry)
            else:
                key = self.create_bitcoin_request(amount, message, expiry, ignore_gap)
                if not key:
                    return
                self._addressModel.init_model()
        except InvoiceError as e:
            self.requestCreateError.emit('fatal',_('Error creating payment request') + ':\n' + str(e))
            return

        assert key is not None
        self.requestCreateSuccess.emit()

        # TODO:copy to clipboard
        #r = self.wallet.get_request(key)
        #content = r.invoice if r.is_lightning() else r.get_address()
        #title = _('Invoice') if is_lightning else _('Address')
        #self.do_copy(content, title=title)

    @pyqtSlot('QString')
    def delete_request(self, key: str):
        self.wallet.delete_request(key)
        self._requestModel.delete_request(key)

    @pyqtSlot('QString', result='QVariant')
    def get_request(self, key: str):
        req = self.wallet.get_request(key)
        return self._requestModel.request_to_model(req)
