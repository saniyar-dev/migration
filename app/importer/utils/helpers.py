import json
import pytz
import re
import secrets
from base64 import b64encode
from hashlib import sha256

from functools import lru_cache
from pathlib import Path
from collections import defaultdict
from datetime import datetime, timezone
from typing import Optional, Tuple, Dict, List

from . import config, logger
from ..models import MarzAdminData, MarzUserData, UserCreate, UserExpireStrategy

USERNAME_REGEXP = r"^\w{3,32}$"


@lru_cache(maxsize=1)
def _load_jwt_secret(json_file: str | Path = config.MARZBAN_USERS_DATA) -> Optional[str]:
    try:
        file_path = Path(json_file)
        if not file_path.exists():
            logger.error(f"Marzban data file not found at: {file_path}")
            return None
        with file_path.open(encoding="utf-8") as file:
            data = json.load(file)
        jwt_entries = data.get("jwt") or []
        if not jwt_entries:
            logger.error("No 'jwt' entries found in Marzban data")
            return None
        return jwt_entries[0].get("secret_key")
    except Exception as e:
        logger.error(f"Error loading JWT secret: {e}")
        return None


def make_marzban_token(username: str, jwt_secret: str) -> str:
    """
    Generate a Marzban-compatible subscription token.

    Format (matches Marzban's `create_subscription_token` in
    `Marzban/app/utils/jwt.py:47-57`):
        b64url(username,unix_ts) + b64url(sha256(token + secret))[:10]
    """
    data = f"{username},{int(datetime.utcnow().timestamp())}"
    data_b64 = b64encode(data.encode("utf-8"), altchars=b"-_").decode("utf-8").rstrip("=")
    sig = b64encode(
        sha256((data_b64 + jwt_secret).encode("utf-8")).digest(), altchars=b"-_"
    ).decode("utf-8")[:10]
    return data_b64 + sig


def gen_key(uuid: str) -> str:
    stripped = uuid.strip('"')
    return stripped.replace("-", "")


def parse_marzban_data(
    json_file: str | Path = config.MARZBAN_USERS_DATA,
) -> Optional[Tuple[List[MarzAdminData], Dict[str, List[MarzUserData]]]]:
    try:
        file_path = Path(json_file)

        if not file_path.exists():
            logger.error(f"Marzban data file not found at: {file_path}")
            return None

        with file_path.open(encoding="utf-8") as file:
            data: Dict[str, List[dict]] = json.load(file)

        if not all(key in data for key in ("users", "admins", "jwt")):
            logger.error(
                "Missing required keys 'users' or 'admins' or 'jwt' in JSON data"
            )
            return None

        admins = [MarzAdminData(**admin) for admin in data["admins"]]
        admins.append(
            MarzAdminData(
                id=99999999,
                username="bear",
                hashed_password="bear",
                created_at=datetime.now(),
                is_sudo=0,
            )
        )
        admin_map = {admin.id: admin.username for admin in admins}

        users_by_admin = defaultdict(list)
        for user in [MarzUserData(**user) for user in data["users"]]:
            users_by_admin[admin_map.get(user.admin_id, "bear")].append(user)

        users_by_admin = dict(users_by_admin)

        logger.info(
            f"Parsed {len(data['users'])} users across {len(users_by_admin)} admin groups"
        )
        return admins, dict(users_by_admin)

    except json.JSONDecodeError as e:
        logger.error(f"Invalid JSON format: {str(e)}")
        return None

    except KeyError as e:
        logger.error(f"Missing required key in data structure: {str(e)}")
        return None

    except Exception as e:
        logger.error(f"Unexpected error parsing Marzban data: {str(e)}")
        logger.exception(e)
        return None


def parse_marz_user(old: MarzUserData, service: int) -> UserCreate:
    if old.data_limit:
        remaining_data = old.data_limit - old.used_traffic
        data_limit = 1024 * 1024 if remaining_data <= 0 else remaining_data
    else:
        data_limit = 0

    tehran_tz = pytz.timezone("Asia/Tehran")
    expire_date = (
        datetime.fromtimestamp(old.expire, tz=timezone.utc)
        .astimezone(tehran_tz)
        .isoformat()
        if old.expire
        else None
    )

    username = old.username.lower().replace("-", "_")

    key = gen_key(old.uuid) if old.uuid is not None else None

    jwt_secret = _load_jwt_secret()
    sub_token = make_marzban_token(old.username, jwt_secret) if jwt_secret else None

    return UserCreate(
        username=username,
        data_limit=data_limit,
        data_limit_reset_strategy=old.data_limit_reset_strategy,
        expire_strategy=(
            UserExpireStrategy.START_ON_FIRST_USE
            if old.status == "on_hold"
            else (
                UserExpireStrategy.FIXED_DATE
                if old.expire
                else UserExpireStrategy.NEVER
            )
        ),
        note=old.note or "",
        usage_duration=old.on_hold_expire_duration if old.status == "on_hold" else None,
        activation_deadline=(
            old.on_hold_timeout.isoformat()
            if old.status == "on_hold" and old.on_hold_timeout
            else None
        ),
        service_ids=[service],
        expire_date=expire_date,
        created_at=old.created_at.isoformat() if old.created_at else None,
        sub_revoked_at=old.sub_revoked_at.isoformat() if old.sub_revoked_at else None,
        key=key,
        sub_token=sub_token,
    )
