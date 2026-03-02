# utils/currency_exchange.py
from currency_converter import CurrencyConverter
from typing import Optional
import os
from pymongo import MongoClient
import logging

logger = logging.getLogger(__name__)


class CurrencyService:
    _converter = None
    _mongo_client = None
    _db = None
    _users_collection = None

    @staticmethod
    def _get_converter():
        if CurrencyService._converter is None:
            CurrencyService._converter = CurrencyConverter()
        return CurrencyService._converter

    @staticmethod
    def _get_users_collection():
        if CurrencyService._users_collection is None:
            CurrencyService._mongo_client = MongoClient(os.getenv("MONGODB_URI"))
            CurrencyService._db = CurrencyService._mongo_client[os.getenv("MONGODB_DB", "birdyai")]
            CurrencyService._users_collection = CurrencyService._db["users"]
        return CurrencyService._users_collection

    @staticmethod
    def get_user_currency(user_id: str) -> str:
        """
        Get user's default currency from database.

        Args:
            user_id: User email/ID to look up (stored in 'user_id' field)

        Returns:
            Currency code (e.g., 'USD', 'EUR')

        Raises:
            ValueError: If user not found, currency not set, or currency not supported
            RuntimeError: If database error occurs
        """
        try:
            users_collection = CurrencyService._get_users_collection()

            # ✅ FIX: Query by 'user_id' field (which stores the email)
            user = users_collection.find_one({"user_id": user_id})

            if not user:
                error_msg = f"User not found with user_id: {user_id}"
                logger.error(f"❌ {error_msg}")
                raise ValueError(error_msg)

            default_currency = user.get("default_currency")

            if not default_currency:
                error_msg = (
                    f"User {user_id} does not have a default_currency"
                )
                logger.error(f"❌ {error_msg}")
                raise ValueError(error_msg)

            # Validate currency is supported
            if default_currency not in CurrencyService.get_currencies():
                error_msg = (
                    f"User {user_id} has unsupported currency: {default_currency}. "
                    f"Supported currencies include: {', '.join(list(CurrencyService.get_currencies())[:20])}..."
                )
                logger.error(f"❌ {error_msg}")
                raise ValueError(error_msg)

            logger.debug(f"✅ Retrieved currency for user {user_id}: {default_currency}")
            return default_currency

        except ValueError:
            # Re-raise ValueError as-is
            raise
        except Exception as e:
            error_msg = f"Database error while fetching currency for user {user_id}: {e!r}"
            logger.error(f"❌ {error_msg}", exc_info=True)
            raise RuntimeError(error_msg) from e

    @staticmethod
    def convert(amount: float, from_currency: str, to_currency: str) -> float:
        """
        Convert amount from one currency to another.

        Args:
            amount: Amount to convert
            from_currency: Source currency code
            to_currency: Target currency code

        Returns:
            Converted amount rounded to 2 decimal places
        """
        try:
            if from_currency == to_currency:
                return amount

            converter = CurrencyService._get_converter()
            converted = converter.convert(amount, from_currency, to_currency)
            return round(converted, 2)
        except Exception as e:
            logger.error(
                f"❌ Currency conversion error: {amount} {from_currency} -> {to_currency}: {e}"
            )
            raise ValueError(
                f"Failed to convert {amount} from {from_currency} to {to_currency}: {str(e)}"
            )

    @staticmethod
    def convert_from_user_currency(amount: float, user_id: str, to_currency: str) -> float:
        """
        Convert amount FROM user's default currency to target currency.

        Args:
            amount: Amount to convert
            user_id: User email/ID to get default currency from
            to_currency: Target currency code

        Returns:
            Converted amount
        """
        from_currency = CurrencyService.get_user_currency(user_id)
        converted_amount = CurrencyService.convert(amount, from_currency, to_currency)
        logger.debug(
            f"💱 Currency Conversion: {amount} {from_currency} ➡️ "
            f"{converted_amount} {to_currency}"
        )
        return converted_amount

    @staticmethod
    def convert_to_user_currency(amount: float, from_currency: str, user_id: str) -> float:
        """
        Convert amount TO user's default currency.

        Args:
            amount: Amount to convert
            from_currency: Source currency code
            user_id: User email/ID to get default currency from

        Returns:
            Converted amount
        """
        to_currency = CurrencyService.get_user_currency(user_id)
        return CurrencyService.convert(amount, from_currency, to_currency)

    @staticmethod
    def get_rate(from_currency: str, to_currency: str) -> float:
        """
        Get exchange rate between two currencies.

        Args:
            from_currency: Source currency code
            to_currency: Target currency code

        Returns:
            Exchange rate (how much 1 unit of from_currency equals in to_currency)
        """
        try:
            if from_currency == to_currency:
                return 1.0
            converter = CurrencyService._get_converter()
            return converter.convert(1, from_currency, to_currency)
        except Exception as e:
            logger.error(f"❌ Error getting rate {from_currency} -> {to_currency}: {e}")
            raise ValueError(
                f"Failed to get exchange rate from {from_currency} to {to_currency}: {str(e)}"
            )

    @staticmethod
    def get_currencies() -> list:
        """
        Get list of available currencies.

        Returns:
            List of currency codes (e.g., ['USD', 'EUR', 'GBP', ...])
        """
        converter = CurrencyService._get_converter()
        return list(converter.currencies)



currency_service = CurrencyService