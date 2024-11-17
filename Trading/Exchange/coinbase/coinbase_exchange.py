#  Drakkar-Software OctoBot-Tentacles
#  Copyright (c) Drakkar-Software, All rights reserved.
#
#  This library is free software; you can redistribute it and/or
#  modify it under the terms of the GNU Lesser General Public
#  License as published by the Free Software Foundation; either
#  version 3.0 of the License, or (at your option) any later version.
#
#  This library is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
#  Lesser General Public License for more details.
#
#  You should have received a copy of the GNU Lesser General Public
#  License along with this library.
import typing
import decimal
import ccxt

import octobot_trading.errors
import octobot_trading.enums as trading_enums
import octobot_trading.constants as trading_constants
import octobot_trading.exchanges as exchanges
import octobot_trading.exchanges.connectors.ccxt.enums as ccxt_enums
import octobot_trading.exchanges.connectors.ccxt.constants as ccxt_constants
import octobot_trading.exchanges.connectors.ccxt.ccxt_connector as ccxt_connector
import octobot_trading.personal_data.orders.order_util as order_util
import octobot_commons.enums as commons_enums
import octobot_commons.constants as commons_constants
import octobot_commons.symbols as commons_symbols
import octobot_commons.logging as logging


def _coinbase_retrier(f):
    async def coinbase_retrier_wrapper(*args, **kwargs):
        last_error = None
        for i in range(0, Coinbase.FAKE_RATE_LIMIT_ERROR_INSTANT_RETRY_COUNT):
            try:
                return await f(*args, **kwargs)
            except (
                octobot_trading.errors.FailedRequest, octobot_trading.errors.RateLimitExceeded, ccxt.BaseError
            ) as err:
                last_error = err
                if Coinbase.INSTANT_RETRY_ERROR_CODE in str(err):
                    # should retry instantly, error on coinbase side
                    logging.get_logger(Coinbase.get_name()).debug(
                        f"{Coinbase.INSTANT_RETRY_ERROR_CODE} error on {f.__name__}(args={args[1:]} kwargs={kwargs}) "
                        f"request, retrying now. Attempt {i+1} / {Coinbase.FAKE_RATE_LIMIT_ERROR_INSTANT_RETRY_COUNT}, "
                        f"error: {err} ({last_error.__class__.__name__})."
                    )
                else:
                    raise
        last_error = last_error or RuntimeError("Unknown Coinbase error")  # to be able to "raise from" in next line
        raise octobot_trading.errors.FailedRequest(
            f"Failed Coinbase request after {Coinbase.FAKE_RATE_LIMIT_ERROR_INSTANT_RETRY_COUNT} "
            f"retries on {f.__name__}(args={args[1:]} kwargs={kwargs}) due "
            f"to {Coinbase.INSTANT_RETRY_ERROR_CODE} error code. "
            f"Last error: {last_error} ({last_error.__class__.__name__})"
        ) from last_error
    return coinbase_retrier_wrapper


class CoinbaseConnector(ccxt_connector.CCXTConnector):

    def _client_factory(self, force_unauth, keys_adapter=None) -> tuple:
        return super()._client_factory(force_unauth, keys_adapter=self._keys_adapter)

    def _keys_adapter(self, key, secret, password, uid, auth_token):
        if auth_token:
            # when auth token is provided, force invalid keys
            return "ANY_KEY", "ANY_SECRET", password, uid, auth_token, "Bearer "
        # CCXT pem key reader is not expecting users to under keys pasted as text from the coinbase UI
        # convert \\n to \n to make this format compatible as well
        if secret and "\\n" in secret:
            secret = secret.replace("\\n", "\n")
        return key, secret, password, uid, None, None

    @_coinbase_retrier
    async def _load_markets(self, client, reload: bool):
        # override for retrier
        await client.load_markets(reload=reload)


class Coinbase(exchanges.RestExchange):
    MAX_PAGINATION_LIMIT: int = 300
    REQUIRES_AUTHENTICATION = True
    IS_SKIPPING_EMPTY_CANDLES_IN_OHLCV_FETCH = True
    DEFAULT_CONNECTOR_CLASS = CoinbaseConnector

    FAKE_RATE_LIMIT_ERROR_INSTANT_RETRY_COUNT = 5
    INSTANT_RETRY_ERROR_CODE = "429"

    FIX_MARKET_STATUS = True

    # text content of errors due to orders not found errors
    EXCHANGE_ORDER_NOT_FOUND_ERRORS: typing.List[typing.Iterable[str]] = [
        # coinbase {"error":"NOT_FOUND","error_details":"order with this orderID was not found",
        #   "message":"order with this orderID was not found"}
        ("not_found", "order")
    ]

    # text content of errors due to api key permissions issues
    EXCHANGE_PERMISSION_ERRORS: typing.List[typing.Iterable[str]] = [
        # coinbase ex: coinbase {"error":"PERMISSION_DENIED",
        # "error_details":"Missing required scopes","message":"Missing required scopes"}
        # ExchangeError('coinbase {"error":"unknown","error_details":"Missing required scopes",
        # "message":"Missing required scopes"}')
        ("missing required scopes", ),
    ]
    # text content of errors due to traded assets for account
    EXCHANGE_ACCOUNT_TRADED_SYMBOL_PERMISSION_ERRORS: typing.List[typing.Iterable[str]] = [
        # ex when trading WBTC/USDC with and account that can't trade it:
        # ccxt.base.errors.BadRequest: target is not enabled for trading
        ("target is not enabled for trading", ),
        # ccxt.base.errors.PermissionDenied: coinbase {"error":"PERMISSION_DENIED","error_details":
        # "User is not allowed to convert crypto","message":"User is not allowed to convert crypto"}
        ("user is not allowed to convert crypto", ),
    ]
    # text content of errors due to exchange internal synch (like when portfolio is not yet up to date after a trade)
    EXCHANGE_INTERNAL_SYNC_ERRORS: typing.List[typing.Iterable[str]] = [
        # BadRequest coinbase {"error":"INVALID_ARGUMENT","error_details":"account is not available","message":"account is not available"}
        ("account is not available", )
    ]
    # text content of errors due to missing fnuds when creating an order (when not identified as such by ccxt)
    EXCHANGE_MISSING_FUNDS_ERRORS: typing.List[typing.Iterable[str]] = [
        ("insufficient balance in source account", )
    ]

    @classmethod
    def get_name(cls):
        return 'coinbase'

    def get_adapter_class(self):
        return CoinbaseCCXTAdapter

    async def get_account_id(self, **kwargs: dict) -> str:
        try:
            # warning might become deprecated
            # https://docs.cloud.coinbase.com/sign-in-with-coinbase/docs/api-users
            user_data = await self.connector.client.v2PrivateGetUser()
            return user_data["data"]["id"]
        except ccxt.BaseError as err:
            self.logger.exception(
                err, True,
                f"Error when fetching {self.get_name()} account id: {err} ({err.__class__.__name__}). "
                f"This is not normal, endpoint might be deprecated, see"
                f"https://docs.cloud.coinbase.com/sign-in-with-coinbase/docs/api-users. "
                f"Using generated account id instead"
            )
            return trading_constants.DEFAULT_ACCOUNT_ID

    @_coinbase_retrier
    async def get_symbol_prices(self, symbol: str, time_frame: commons_enums.TimeFrames, limit: int = None,
                                **kwargs: dict) -> typing.Optional[list]:
        return await super().get_symbol_prices(
            symbol, time_frame, **self._get_ohlcv_params(time_frame, limit, **kwargs)
        )

    @_coinbase_retrier
    async def get_recent_trades(self, symbol, limit=50, **kwargs):
        # override for retrier
        return await super().get_recent_trades(symbol, limit=limit, **kwargs)

    @_coinbase_retrier
    async def get_price_ticker(self, symbol: str, **kwargs: dict) -> typing.Optional[dict]:
        # override for retrier
        return await super().get_price_ticker(symbol, **kwargs)

    @_coinbase_retrier
    async def get_all_currencies_price_ticker(self, **kwargs: dict) -> typing.Optional[dict[str, dict]]:
        # override for retrier
        return await super().get_all_currencies_price_ticker(**kwargs)

    async def create_order(self, order_type: trading_enums.TraderOrderType, symbol: str, quantity: decimal.Decimal,
                           price: decimal.Decimal = None, stop_price: decimal.Decimal = None,
                           side: trading_enums.TradeOrderSide = None, current_price: decimal.Decimal = None,
                           reduce_only: bool = False, params: dict = None) -> typing.Optional[dict]:
        # ccxt is converting quantity using price, make sure it's available
        if order_type is trading_enums.TraderOrderType.BUY_MARKET and not current_price:
            raise octobot_trading.errors.NotSupported(f"current_price is required for {order_type} orders")
        return await super().create_order(order_type, symbol, quantity,
                                          price=price, stop_price=stop_price,
                                          side=side, current_price=current_price,
                                          reduce_only=reduce_only, params=params)

    @_coinbase_retrier
    async def cancel_order(
        self, exchange_order_id: str, symbol: str, order_type: trading_enums.TraderOrderType, **kwargs: dict
    ) -> trading_enums.OrderStatus:
        # override for retrier
        return await super().cancel_order(exchange_order_id, symbol, order_type, **kwargs)

    @_coinbase_retrier
    async def get_balance(self, **kwargs: dict):
        if "v3" not in kwargs:
            # use v3 to get free and total amounts (default is only returning free amounts)
            kwargs["v3"] = True
        return await super().get_balance(**kwargs)

    @_coinbase_retrier
    async def _create_order_with_retry(self, order_type, symbol, quantity: decimal.Decimal,
                                       price: decimal.Decimal, stop_price: decimal.Decimal,
                                       side: trading_enums.TradeOrderSide,
                                       current_price: decimal.Decimal,
                                       reduce_only: bool, params) -> dict:
        # override for retrier
        return await super()._create_order_with_retry(
            order_type=order_type, symbol=symbol, quantity=quantity, price=price,
            stop_price=stop_price, side=side, current_price=current_price,
            reduce_only=reduce_only, params=params
        )

    @_coinbase_retrier
    async def get_open_orders(self, symbol=None, since=None, limit=None, **kwargs) -> list:
        # override for retrier
        return await super().get_open_orders(symbol=symbol, since=since, limit=limit, **kwargs)

    @_coinbase_retrier
    async def get_order(self, exchange_order_id: str, symbol: str = None, **kwargs: dict) -> dict:
        # override for retrier
        return await super().get_order(exchange_order_id, symbol=symbol, **kwargs)

    def _get_ohlcv_params(self, time_frame, input_limit, **kwargs):
        limit = input_limit
        if not input_limit or input_limit > self.MAX_PAGINATION_LIMIT:
            limit = min(self.MAX_PAGINATION_LIMIT, input_limit) if input_limit else self.MAX_PAGINATION_LIMIT
        if "since" not in kwargs:
            time_frame_sec = commons_enums.TimeFramesMinutes[time_frame] * commons_constants.MSECONDS_TO_MINUTE
            to_time = self.connector.client.milliseconds()
            kwargs["since"] = to_time - (time_frame_sec * limit)
            kwargs["limit"] = limit
        return kwargs

    def is_market_open_for_order_type(self, symbol: str, order_type: trading_enums.TraderOrderType) -> bool:
        """
        Override if necessary
        """
        market_status_info = self.get_market_status(symbol, with_fixer=False).get(ccxt_constants.CCXT_INFO, {})
        trade_order_type = order_util.get_trade_order_type(order_type)
        try:
            if trade_order_type is trading_enums.TradeOrderType.MARKET:
                return not market_status_info["limit_only"]
            if trade_order_type is trading_enums.TradeOrderType.LIMIT:
                return not market_status_info["cancel_only"]
        except KeyError as err:
            self.logger.exception(
                err,
                True,
                f"Can't check {self.get_name()} market opens status for order type: missing {err} "
                f"in market status info. {self.get_name()} API probably changed. Considering market as open. "
                f"market_status_info: {market_status_info}"
            )
        return True


class CoinbaseCCXTAdapter(exchanges.CCXTAdapter):

    def _register_exchange_fees(self, order_or_trade):
        super()._register_exchange_fees(order_or_trade)
        try:
            fees = order_or_trade[trading_enums.ExchangeConstantsOrderColumns.FEE.value]
            if not fees[trading_enums.FeePropertyColumns.CURRENCY.value]:
                # fees currency are not provided, they are always in quote on Coinbase
                fees[trading_enums.FeePropertyColumns.CURRENCY.value] = commons_symbols.parse_symbol(
                    order_or_trade[trading_enums.ExchangeConstantsOrderColumns.SYMBOL.value]
                ).quote
        except (KeyError, TypeError):
            pass

    def fix_order(self, raw, **kwargs):
        """
        Handle 'order_type': 'UNKNOWN_ORDER_TYPE in coinbase order response (translated into None in ccxt order type)
        ex:
        {'info': {'order_id': 'd7471b4e-960e-4c92-bdbf-755cb92e176b', 'product_id': 'AAVE-USD',
        'user_id': '9868efd7-90e1-557c-ac0e-f6b943d471ad', 'order_configuration': {'limit_limit_gtc':
        {'base_size': '6.798', 'limit_price': '110.92', 'post_only': False}}, 'side': 'BUY',
        'client_order_id': '465ead64-6272-4e92-97e2-59653de3ca24', 'status': 'OPEN', 'time_in_force':
        'GOOD_UNTIL_CANCELLED', 'created_time': '2024-03-02T03:04:11.070126Z', 'completion_percentage':
        '0', 'filled_size': '0', 'average_filled_price': '0', 'fee': '', 'number_of_fills': '0', 'filled_value': '0',
        'pending_cancel': False, 'size_in_quote': False, 'total_fees': '0', 'size_inclusive_of_fees': False,
        'total_value_after_fees': '757.05029664', 'trigger_status': 'INVALID_ORDER_TYPE', 'order_type':
        'UNKNOWN_ORDER_TYPE', 'reject_reason': 'REJECT_REASON_UNSPECIFIED', 'settled': False, 'product_type':
        'SPOT', 'reject_message': '', 'cancel_message': '', 'order_placement_source': 'RETAIL_ADVANCED',
        'outstanding_hold_amount': '757.05029664', 'is_liquidation': False, 'last_fill_time': None,
        'edit_history': [], 'leverage': '', 'margin_type': 'UNKNOWN_MARGIN_TYPE'}, 'clientOrderId':
        '465ead64-6272-4e92-97e2-59653de3ca24', 'timestamp': 1709348651.07, 'datetime': '2024-03-02T03:04:11.070126Z',
        'lastTradeTimestamp': None, 'symbol': 'AAVE/USD', 'type': None, 'timeInForce': 'GTC', 'postOnly': False,
        'side': 'buy', 'price': 110.92, 'stopPrice': None, 'triggerPrice': None, 'amount': 6.798, 'filled': 0.0,
        'remaining': 6.798, 'cost': 0.0, 'average': None, 'status': 'open', 'fee': {'cost': '0', 'currency': 'USD',
        'exchange_original_cost': '0', 'is_from_exchange': True}, 'trades': [],
        'fees': [{'cost': 0.0, 'currency': 'USD'}], 'lastUpdateTimestamp': None, 'reduceOnly': None,
        'takeProfitPrice': None, 'stopLossPrice': None, 'exchange_id': 'd7471b4e-960e-4c92-bdbf-755cb92e176b'}
        """
        fixed = super().fix_order(raw, **kwargs)
        if fixed[ccxt_enums.ExchangeOrderCCXTColumns.TYPE.value] is None:
            if fixed[ccxt_enums.ExchangeOrderCCXTColumns.STOP_PRICE.value] is not None:
                # stop price set: stop order
                order_type = trading_enums.TradeOrderType.STOP_LOSS.value
            elif fixed[ccxt_enums.ExchangeOrderCCXTColumns.PRICE.value] is None:
                # price not set: market order
                order_type = trading_enums.TradeOrderType.MARKET.value
            else:
                # price is set and stop price is not: limit order
                order_type = trading_enums.TradeOrderType.LIMIT.value
            fixed[trading_enums.ExchangeConstantsOrderColumns.TYPE.value] = order_type
        if fixed[ccxt_enums.ExchangeOrderCCXTColumns.STATUS.value] == "PENDING":
            fixed[ccxt_enums.ExchangeOrderCCXTColumns.STATUS.value] = trading_enums.OrderStatus.PENDING_CREATION.value
        if fixed[ccxt_enums.ExchangeOrderCCXTColumns.STATUS.value] == "CANCEL_QUEUED":
            fixed[ccxt_enums.ExchangeOrderCCXTColumns.STATUS.value] = trading_enums.OrderStatus.PENDING_CANCEL.value
        # sometimes amount is not set
        if not fixed[ccxt_enums.ExchangeOrderCCXTColumns.AMOUNT.value] \
                and fixed[ccxt_enums.ExchangeOrderCCXTColumns.FILLED.value]:
            fixed[ccxt_enums.ExchangeOrderCCXTColumns.AMOUNT.value] = \
                fixed[ccxt_enums.ExchangeOrderCCXTColumns.FILLED.value]
        return fixed

    def fix_trades(self, raw, **kwargs):
        raw = super().fix_trades(raw, **kwargs)
        for trade in raw:
            trade[trading_enums.ExchangeConstantsOrderColumns.STATUS.value] = trading_enums.OrderStatus.CLOSED.value
            try:
                if trade[trading_enums.ExchangeConstantsOrderColumns.AMOUNT.value] is None and \
                        trade[trading_enums.ExchangeConstantsOrderColumns.COST.value] and \
                        trade[trading_enums.ExchangeConstantsOrderColumns.PRICE.value]:
                    # convert amount to have the same units as every other exchange
                    trade[trading_enums.ExchangeConstantsOrderColumns.AMOUNT.value] = (
                            trade[trading_enums.ExchangeConstantsOrderColumns.COST.value] /
                            trade[trading_enums.ExchangeConstantsOrderColumns.PRICE.value]
                    )
            except KeyError:
                pass
        return raw
