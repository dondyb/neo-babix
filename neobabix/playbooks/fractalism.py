from asyncio import Lock
from logging import Logger
from os import environ
from decimal import Decimal

from ccxt import Exchange, TRUNCATE

from neobabix.playbooks.playbook import Playbook
from neobabix.strategies.strategy import Actions
from neobabix.notifications.notification import Notification
from neobabix.indicators.billwilliams import UpFractal, DownFractal
from neobabix.constants import BETWEEN_ORDERS_SLEEP

import asyncio


class Fractalism(Playbook):
    __name__ = 'Fractalism Playbook'

    """
        This playbook gets stop prices from the last valid fractal.

        Entry:
            - Buy asset using market order
        Exit:
            - Immediately send an exit limit order at a predetermined percentage based on the buying price + fee
            - Immediately send a stop limit order based on the last valid fractal

        Env Vars:
            - TAKE_PROFIT_IN_PERCENT - required - float - ex: 1.0
            - MODAL_DUID - required - float - ex: 1000.0
            - PRICE_DECIMAL_PLACES - required - integer - ex: 0
    """

    def __init__(self, action: Actions, exchange: Exchange, trade_lock: Lock, logger: Logger, symbol: str,
                 timeframe: str, notification: Notification, ohlcv, recursive: bool = False, leverage: int = None):
        super().__init__(action, exchange, trade_lock, logger, symbol, timeframe, notification, recursive, leverage,
                         ohlcv)

        self.tp_in_percent = environ.get('TAKE_PROFIT_IN_PERCENT')
        if not self.tp_in_percent:
            raise NotImplementedError('Required env var TAKE_PROFIT_IN_PERCENT must be set')
        self.tp_in_percent = float(self.tp_in_percent)

        self.modal_duid = environ.get('MODAL_DUID')
        if not self.modal_duid:
            raise NotImplementedError('Required env var MODAL_DUID must be set')

        self.price_decimal_places = int(environ.get('PRICE_DECIMAL_PLACES'))
        if not self.price_decimal_places and not self.price_decimal_places == 0:
            raise NotImplementedError('Required env var PRICE_DECIMAL_PLACES must be set')

        self.up_fractals = UpFractal(highs=ohlcv.get('highs'))
        self.down_fractals = DownFractal(lows=ohlcv.get('lows'))

    @property
    def last_valid_up_fractal(self):
        current_price = float(self.ohlcv.get('closes')[-1])
        fractals = list(filter(lambda x: x is not None and x > current_price, self.up_fractals))
        if len(fractals) == 0:
            return None

        return float(fractals[-1])

    @property
    def last_valid_down_fractal(self):
        current_price = float(self.ohlcv.get('closes')[-1])
        fractals = list(filter(lambda x: x is not None and x < current_price, self.down_fractals))
        if len(fractals) == 0:
            return None

        return float(fractals[-1])

    @property
    def exit_price(self):
        if self.entry_price is None:
            return None

        exit_price = None
        if self.action == Actions.LONG:
            exit_price = Decimal(self.entry_price) * Decimal(self.tp_in_percent + 100.0) / Decimal(100)
            exit_price = float(exit_price)
            exit_price = self.exchange.decimal_to_precision(n=exit_price,
                                                            rounding_mode=TRUNCATE,
                                                            precision=self.price_decimal_places)
        elif self.action == Actions.SHORT:
            exit_price = Decimal(self.entry_price) * Decimal(100.0 - self.tp_in_percent) / Decimal(100)
            exit_price = float(exit_price)
            exit_price = self.exchange.decimal_to_precision(n=exit_price,
                                                            rounding_mode=TRUNCATE,
                                                            precision=self.price_decimal_places)
        return exit_price

    @property
    def stop_price(self):
        if self.entry_price is None:
            return None

        stop_price = None
        if self.action == Actions.LONG:
            stop_price = self.exchange.decimal_to_precision(n=self.last_valid_down_fractal,
                                                            rounding_mode=TRUNCATE,
                                                            precision=self.price_decimal_places)
        elif self.action == Actions.SHORT:
            stop_price = self.exchange.decimal_to_precision(n=self.last_valid_up_fractal,
                                                            rounding_mode=TRUNCATE,
                                                            precision=self.price_decimal_places)
        return stop_price

    @property
    def stop_action_price(self):
        if self.entry_price is None:
            return None

        stop_action_price = None
        if self.action == Actions.LONG:
            stop_action_price = self.last_valid_down_fractal - 10.0
            stop_action_price = self.exchange.decimal_to_precision(n=stop_action_price,
                                                                   rounding_mode=TRUNCATE,
                                                                   precision=self.price_decimal_places)
        elif self.action == Actions.SHORT:
            stop_action_price = self.last_valid_up_fractal + 10.0
            stop_action_price = self.exchange.decimal_to_precision(n=stop_action_price,
                                                                   rounding_mode=TRUNCATE,
                                                                   precision=self.price_decimal_places)

        return stop_action_price

    async def entry(self):
        self.info('Going to execute entry')
        if self.leverage is not None:
            self.info(f'Setting leverage to {self.leverage}x')
            await self.set_leverage(leverage=self.leverage)

        if self.action == Actions.LONG:
            self.info('Entering a LONG position')
            self.order_entry = await self.market_buy_order(amount=self.modal_duid)
        elif self.action == Actions.SHORT:
            self.info('Entering a SHORT position')
            self.order_entry = await self.market_sell_order(amount=self.modal_duid)

    async def after_entry(self):
        self.info(f'Successfully entered a trade')
        self.info(f'Modal Duid: {self.modal_duid}')
        self.info(f'Entry Price: {self.order_entry.get("price")}')

        await self.notification.send_entry_notification(entry_price=str(self.order_entry.get('price')),
                                                        modal_duid=str(self.modal_duid))

        self.info(f'Sleeping for {BETWEEN_ORDERS_SLEEP} seconds before submitting exit/stop orders')
        await asyncio.sleep(BETWEEN_ORDERS_SLEEP)

    async def exit(self):
        self.info('Going to execute exit')

        exit_order_method = stop_order_method = None

        if self.action == Actions.LONG:
            exit_order_method = self.limit_sell_order
            stop_order_method = self.limit_stop_sell_order
        elif self.action == Actions.SHORT:
            exit_order_method = self.limit_buy_order
            stop_order_method = self.limit_stop_buy_order

        self.info(f'Exit Price: {self.exit_price}')
        self.info(f'Stop Price: {self.stop_price}')
        self.info(f'Stop Sell Price: {self.stop_action_price}')

        # TP
        self.order_exit = await exit_order_method(amount=self.modal_duid,
                                                  price=self.exit_price)

        self.info(f'Sleeping for {BETWEEN_ORDERS_SLEEP} seconds before submitting stop order')
        await asyncio.sleep(BETWEEN_ORDERS_SLEEP)

        # Stop
        self.order_stop = await stop_order_method(amount=self.modal_duid,
                                                  stop_price=self.stop_price,
                                                  stop_action_price=self.stop_action_price,
                                                  base_price=self.entry_price)

        self.info('TP and SL orders are created')

    async def after_exit(self):
        self.info('Done creating orders, polling for exits')

        await self.notification.send_exit_notification(entry_price=str(self.entry_price),
                                                       modal_duid=str(self.modal_duid),
                                                       exit_price=str(self.exit_price),
                                                       stop_limit_price=str(self.stop_price),
                                                       settled=False)

        poll_result = await self.poll_results()

        self.order_exit = poll_result.get('exit_order')
        self.info(f'Exit order status: {self.order_exit.get("status")}')

        self.order_stop = poll_result.get('stop_order')
        self.info(f'Stop order status: {self.order_stop.get("status")}')

        entry_price = Decimal(self.entry_price)
        exit_price = None
        won = None

        # Cancel the other order
        if self.order_exit.get('status') == 'closed':
            self.info('Cancelling stop order')
            await self.cancel_order(order_id=self.order_stop.get('id'))
            exit_price = Decimal(self.exit_price)
            won = True
        elif self.order_stop.get('status') == 'closed':
            self.info('Cancelling exit order')
            exit_price = Decimal(self.stop_price)
            await self.cancel_order(order_id=self.order_exit.get('id'))
            won = False

        if won is None:
            self.info('User canceled the orders, breaking..')
            self.info('Sending exit notification')
            await self.notification.send_exit_notification(entry_price=str(self.entry_price),
                                                           exit_price=str(self.exit_price),
                                                           stop_limit_price=str(self.stop_price),
                                                           modal_duid=str(self.modal_duid),
                                                           settled=True,
                                                           pnl_in_percent='n/a')
            return

        pnl = None

        if self.action == Actions.LONG and won:
            pnl = exit_price / entry_price * Decimal(100) - Decimal(100)
        elif self.action == Actions.LONG and not won:
            pnl = (entry_price / exit_price * Decimal(100) - Decimal(100)) * Decimal(-1)
        elif self.action == Actions.SHORT and won:
            pnl = entry_price / exit_price * Decimal(100) - Decimal(100)
        elif self.action == Actions.SHORT and not won:
            pnl = (exit_price / entry_price * Decimal(100) - Decimal(100)) * Decimal(-1)

        pnl = float(pnl)

        self.info('Sending exit notification')
        await self.notification.send_exit_notification(entry_price=str(self.entry_price),
                                                       exit_price=str(self.exit_price),
                                                       stop_limit_price=str(self.stop_price),
                                                       modal_duid=str(self.modal_duid),
                                                       settled=True,
                                                       pnl_in_percent=pnl)
