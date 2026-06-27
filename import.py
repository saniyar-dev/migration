import asyncio

from pathlib import Path
from app.importer.utils import helpers, logger, config, MarzneshinClient
from app.importer.models import ServiceCreate, AdminCreate, AdminUpdate

STANDARD_SERVICE_NAME = "standard"


async def get_or_create_standard_service(api: MarzneshinClient, inbound_ids: list[int]) -> int | None:
    existing = await api.list_services()
    if existing and existing.items:
        for service in existing.items:
            if service.name == STANDARD_SERVICE_NAME:
                logger.info(f"Reusing existing '{STANDARD_SERVICE_NAME}' service (id={service.id})")
                return service.id

    logger.info(f"Creating '{STANDARD_SERVICE_NAME}' service...")
    service = await api.create_service(
        ServiceCreate(
            name=STANDARD_SERVICE_NAME,
            inbound_ids=inbound_ids,
        )
    )
    if not service:
        logger.error(f"Failed to create '{STANDARD_SERVICE_NAME}' service")
        return None
    logger.info(f"Created '{STANDARD_SERVICE_NAME}' service (id={service.id})")
    return service.id


async def main():
    file_path = Path("marzban.json")

    if not file_path.exists():
        logger.error(f"Marzban data file not found at: {file_path}")
        return None

    logger.info("🚀 Starting Marzneshin Migration Import process...")
    admins, users_by_admin = helpers.parse_marzban_data()
    if not admins or not users_by_admin:
        logger.error("Failed to read marzban.json")
        return None

    async with MarzneshinClient() as api:
        logger.info("Checking admin sudo access...")
        sudo_check = await api.login(
            config.MARZNESHIN_USERNAME, config.MARZNESHIN_PASSWORD
        )
        if not sudo_check or not sudo_check.is_sudo:
            logger.error("Admin access is not valid! Need sudo privileges.")
            return

        logger.info("Checking available inbounds...")
        inbounds = await api.get_inbounds()
        if not inbounds or not inbounds.items:
            logger.error(
                "No inbounds found! Please add at least one inbound before migration."
            )
            return

        standard_service_id = await get_or_create_standard_service(
            api, [inbound.id for inbound in inbounds.items]
        )
        if not standard_service_id:
            return

        logger.info(f"Starting admin migration process for {len(admins)} admins...")
        for admin in admins:
            success = False
            for attempt in range(3):
                logger.info(
                    f"Processing admin: {admin.username} (Attempt {attempt + 1}/3)"
                )

                try:
                    check_admin = await api.get_admin(admin.username)

                    if not check_admin:
                        logger.info(f"Creating new admin account: {admin.username}")
                        admin_account = await api.create_admin(
                            AdminCreate(
                                username=admin.username,
                                password=f"{admin.username}{admin.username}",
                                service_ids=[standard_service_id],
                                all_services_access=True,
                            )
                        )
                    else:
                        if check_admin.is_sudo:
                            logger.error(f"Cannot modify sudo admin: {admin.username}")
                            break

                        logger.info(f"Updating existing admin: {admin.username}")
                        admin_account = await api.update_admin(
                            AdminUpdate(
                                username=admin.username,
                                password=f"{admin.username}{admin.username}",
                                service_ids=[standard_service_id],
                                all_services_access=True,
                            )
                        )

                    if not admin_account:
                        logger.error(
                            f"Failed to create/update admin account: {admin.username}"
                        )
                        continue

                    logger.info(f"Successfully processed admin: {admin.username}")
                    success = True
                    break

                except Exception as e:
                    logger.error(
                        f"Error processing admin {admin.username} (Attempt {attempt + 1}/3): {str(e)}"
                    )
                    if attempt < 2:
                        logger.info(f"Retrying admin {admin.username}...")
                        continue

            if not success:
                logger.error(
                    f"Failed to process admin {admin.username} after 3 attempts"
                )

        logger.info("Starting user migration process...")
        for admin, users in users_by_admin.items():
            logger.info(f"Processing users for admin: {admin} ({len(users)} users)")

            async with MarzneshinClient() as api:
                logger.info(f"Logging in as admin: {admin}")
                check_login = await api.login(admin, f"{admin}{admin}")
                if not check_login:
                    logger.error(f"Failed to login as admin: {admin}")
                    continue

                for user in users:
                    logger.info(f"Processing user: {user.username}")

                    try:
                        new_user = helpers.parse_marz_user(user, standard_service_id)
                        created_user = await api.create_user(new_user)
                        if not created_user:
                            logger.error(f"Failed to create user: {user.username}")
                            continue

                        logger.info(f"Successfully created user: {user.username}")

                    except Exception as e:
                        logger.error(f"Error creating user {user.username}: {str(e)}")
                        continue

    logger.info("Migration Import process completed!")


if __name__ == "__main__":
    asyncio.run(main())
