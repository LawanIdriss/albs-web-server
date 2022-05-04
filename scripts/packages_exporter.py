import re

import aiohttp
import argparse
import asyncio
import json
import logging
import os
import sys
import typing
import urllib.parse
from pathlib import Path

import jmespath
from plumbum import local
import sqlalchemy
from sqlalchemy.future import select
from sqlalchemy.orm import selectinload
from syncer import sync

sys.path.append(os.path.dirname(os.path.dirname(__file__)))


from alws import database
from alws import models
from alws.config import settings
from alws.utils.pulp_client import PulpClient
from errata_migrator import update_updateinfo


KNOWN_SUBKEYS_CONFIG = os.path.abspath(os.path.expanduser(
    '~/config/known_subkeys.json'))


def parse_args():
    parser = argparse.ArgumentParser(
        'packages_exporter',
        description='Packages exporter script. Exports repositories from Pulp'
                    'and transfer them to the filesystem'
    )
    parser.add_argument('-names', '--platform-names',
                        type=str, nargs='+', required=False,
                        help='List of platform names to export')
    parser.add_argument('-repos', '--repo-ids',
                        type=int, nargs='+', required=False,
                        help='List of repo ids to export')
    parser.add_argument('-a', '--arches', type=str, nargs='+',
                        required=False, help='List of arches to export')
    parser.add_argument('-id', '--release-id', type=int,
                        required=False, help='Extract repos by release_id')
    parser.add_argument(
        '-distr', '--distribution', type=str, required=False,
        help='Check noarch packages by distribution'
    )
    parser.add_argument(
        '-copy', '--copy-noarch-packages', action='store_true',
        default=False, required=False,
        help='Copy noarch packages from x86_64 repos into others',
    )
    parser.add_argument(
        '-show-differ', '--show-differ-packages', action='store_true',
        default=False, required=False,
        help='Shows only packages that have different checksum',
    )
    parser.add_argument('-check', '--only-check-noarch', action='store_true',
                        default=False, required=False,
                        help='Only check noarch packages without copying')
    return parser.parse_args()


class Exporter:
    def __init__(
        self,
        pulp_client,
        copy_noarch_packages,
        only_check_noarch,
        show_differ_packages,
    ):
        logging.basicConfig(level=logging.INFO)
        self.logger = logging.getLogger('packages-exporter')
        self.pulp_client = pulp_client
        self.createrepo_c = local['createrepo_c']
        self.modifyrepo_c = local['modifyrepo_c']
        self.copy_noarch_packages = copy_noarch_packages
        self.only_check_noarch = only_check_noarch
        self.show_differ_packages = show_differ_packages
        self.headers = {
            'Authorization': f'Bearer {settings.sign_server_token}',
        }
        self.pulp_system_user = 'pulp'
        self.current_user = os.getlogin()
        self.export_error_file = os.path.abspath(
            os.path.expanduser('~/export.err'))
        if os.path.exists(self.export_error_file):
            os.remove(self.export_error_file)
        self.known_subkeys = {}
        if os.path.exists(KNOWN_SUBKEYS_CONFIG):
            with open(KNOWN_SUBKEYS_CONFIG, 'rt') as f:
                self.known_subkeys = json.load(f)

    async def make_request(self, method: str, endpoint: str,
                           params: dict = None, data: dict = None):
        full_url = urllib.parse.urljoin(settings.sign_server_url, endpoint)
        async with aiohttp.ClientSession(headers=self.headers,
                                         raise_for_status=True) as session:
            async with session.request(method, full_url,
                                       json=data, params=params) as response:
                json_data = await response.read()
                json_data = json.loads(json_data)
                return json_data

    async def create_filesystem_exporters(
            self, repository_ids: typing.List[int],
            get_publications: bool = False
    ):

        export_data = []

        async with database.Session() as db:
            query = select(models.Repository).where(
                models.Repository.id.in_(repository_ids))
            result = await db.execute(query)
            repositories = list(result.scalars().all())

        for repo in repositories:
            export_path = str(Path(
                settings.pulp_export_path, repo.export_path, 'Packages'))
            exporter_name = f'{repo.name}-{repo.arch}-debug' if repo.debug \
                else f'{repo.name}-{repo.arch}'
            fs_exporter_href = await self.pulp_client.create_filesystem_exporter(
                exporter_name, export_path)

            repo_latest_version = await self.pulp_client.get_repo_latest_version(
                repo.pulp_href
            )
            repo_exporter_dict = {
                'repo_id': repo.id,
                'repo_latest_version': repo_latest_version,
                'exporter_name': exporter_name,
                'export_path': export_path,
                'exporter_href': fs_exporter_href
            }
            if get_publications:
                publications = self.pulp_client.get_rpm_publications(
                    repository_version_href=repo_latest_version,
                    include_fields=['pulp_href']
                )
                if publications:
                    publication_href = publications[0].get('pulp_href')
                    repo_exporter_dict['publication_href'] = publication_href

            export_data.append(repo_exporter_dict)
        return export_data

    async def sign_repomd_xml(self, data):
        endpoint = 'sign-tasks/sync_sign_task/'
        return await self.make_request('POST', endpoint, data=data)

    async def get_sign_keys(self):
        endpoint = 'sign-keys/'
        return await self.make_request('GET', endpoint)

    async def export_repositories(self, repo_ids: list) -> typing.List[str]:
        exporters = await self.create_filesystem_exporters(repo_ids)
        exported_paths = []
        for exporter in exporters:
            self.logger.info('Exporting repository using following data: %s',
                             str(exporter))
            export_path = exporter['export_path']
            exported_paths.append(export_path)
            href = exporter['exporter_href']
            repository_version = exporter['repo_latest_version']
            await self.pulp_client.export_to_filesystem(
                href, repository_version)
        return exported_paths

    async def repomd_signer(self, repodata_path, key_id):
        string_repodata_path = str(repodata_path)
        if key_id is None:
            self.logger.info('Cannot sign repomd.xml in %s, missing GPG key',
                             string_repodata_path)
            return

        with open(os.path.join(repodata_path, 'repomd.xml'), 'rt') as f:
            file_content = f.read()
        sign_data = {
            "content": file_content,
            "pgp_keyid": key_id,
        }
        result = await self.sign_repomd_xml(sign_data)
        result_data = result.get('asc_content')
        if result_data is None:
            self.logger.error('repomd.xml in %s is failed to sign:\n%s',
                              string_repodata_path, result['error'])
            return

        repodata_path = os.path.join(repodata_path, 'repomd.xml.asc')
        with open(repodata_path, 'w') as file:
            file.writelines(result_data)
        self.logger.info('repomd.xml in %s is signed', string_repodata_path)

    async def retrieve_all_packages_from_pulp(
        self,
        latest_repo_version: str
    ) -> typing.List[typing.Dict[str, str]]:
        endpoint = 'pulp/api/v3/content/rpm/packages/'
        params = {
            'arch': 'noarch',
            'fields': ','.join(('name', 'version', 'release',
                                'sha256', 'pulp_href')),
            'repository_version': latest_repo_version,
        }
        response = await self.pulp_client.request('GET', endpoint,
                                                  params=params)
        packages = response['results']
        while response.get('next'):
            new_url = response.get('next')
            parsed_url = urllib.parse.urlsplit(new_url)
            new_url = parsed_url.path + '?' + parsed_url.query
            response = await self.pulp_client.request('GET', new_url)
            packages.extend(response['results'])
        return packages

    async def copy_noarch_packages_from_x86_64_repo(
        self,
        source_repo_name: str,
        source_repo_packages: typing.List[dict],
        destination_repo_name: str,
        destination_repo_href: str,
    ) -> None:

        destination_repo_packages = await self.retrieve_all_packages_from_pulp(
            await self.pulp_client.get_repo_latest_version(
                destination_repo_href))

        packages_to_add = []
        packages_to_remove = []
        add_msg = '%s added from "%s" repo into "%s" repo'
        replace_msg = '%s replaced in "%s" repo from "%s" repo'
        if self.only_check_noarch:
            add_msg = '%s can be added from "%s" repo into "%s" repo'
            replace_msg = '%s can be replaced in "%s" repo from "%s" repo'
        for package_dict in source_repo_packages:
            pkg_name = package_dict['name']
            pkg_version = package_dict['version']
            pkg_release = package_dict['release']
            is_modular = '.module_el' in pkg_release
            full_name = f'{pkg_name}-{pkg_version}-{pkg_release}.noarch.rpm'
            compared_pkg = next((
                pkg for pkg in destination_repo_packages
                if all((pkg['name'] == pkg_name,
                        pkg['version'] == pkg_version,
                        pkg['release'] == pkg_release))
            ), None)
            if compared_pkg is None:
                if is_modular or self.show_differ_packages:
                    continue
                packages_to_add.append(package_dict['pulp_href'])
                self.logger.info(add_msg, full_name, source_repo_name,
                                 destination_repo_name)
                continue
            if package_dict['sha256'] != compared_pkg['sha256']:
                packages_to_remove.append(compared_pkg['pulp_href'])
                packages_to_add.append(package_dict['pulp_href'])
                self.logger.info(replace_msg, full_name, destination_repo_name,
                                 source_repo_name)

        if packages_to_add and not self.only_check_noarch:
            await self.pulp_client.modify_repository(
                destination_repo_href,
                add=packages_to_add,
                remove=packages_to_remove,
            )
            await self.pulp_client.create_rpm_publication(
                destination_repo_href,
            )

    async def prepare_and_execute_async_tasks(
        self,
        source_repo_dict: dict,
        destination_repo_dict: dict,
    ) -> None:
        tasks = []
        if not self.copy_noarch_packages:
            self.logger.info('Skip copying noarch packages')
            return
        for source_repo_name, repo_data in source_repo_dict.items():
            repo_href, source_is_debug = repo_data
            source_repo_packages = await self.retrieve_all_packages_from_pulp(
                await self.pulp_client.get_repo_latest_version(repo_href),
            )
            for dest_repo_name, dest_repo_data in destination_repo_dict.items():
                dest_repo_href, dest_repo_is_debug = dest_repo_data
                if source_is_debug != dest_repo_is_debug:
                    continue
                tasks.append(self.copy_noarch_packages_from_x86_64_repo(
                    source_repo_name=source_repo_name,
                    source_repo_packages=source_repo_packages,
                    destination_repo_name=dest_repo_name,
                    destination_repo_href=dest_repo_href,
                ))
        self.logger.info('Start checking and copying noarch packages in repos')
        await asyncio.gather(*tasks)

    def get_full_repo_name(self, repo: models.Repository) -> str:
        return f"{repo.name}-{'debuginfo-' if repo.debug else ''}{repo.arch}"

    async def check_noarch_in_user_distribution_repos(self, distr_name: str):
        async with database.Session() as db:
            db_distr = await db.execute(select(models.Distribution).where(
                models.Distribution.name == distr_name,
            ).options(selectinload(models.Distribution.repositories)))
        db_distr = db_distr.scalars().first()
        repos_x86_64 = {}
        other_repos = {}
        for repo in db_distr.repositories:
            if repo.arch == 'src':
                continue
            if repo.arch == 'x86_64':
                repos_x86_64[repo.name] = (repo.pulp_href, repo.debug)
            else:
                other_repos[repo.name] = (repo.pulp_href, repo.debug)
        await self.prepare_and_execute_async_tasks(repos_x86_64, other_repos)

    def check_rpms_signature(self, repository_path: str, sign_keys: list):
        key_ids_lower = [i.keyid.lower() for i in sign_keys]
        signature_regex = re.compile(
            r'(Signature[\s:]+)(.*Key ID )?(?P<key_id>(\()?\w+(\))?)',
            re.IGNORECASE)
        errored_packages = set()
        no_signature_packages = set()
        wrong_signature_packages = set()
        for package in os.listdir(repository_path):
            package_path = os.path.join(repository_path, package)
            if not package_path.endswith('.rpm'):
                self.logger.debug('Skipping non-RPM file or directory: %s',
                                  package_path)
                continue
            args = ('rpm', '-qip', package_path)
            exit_code, out, err = local['sudo'].run(args=args, retcode=None)
            if exit_code != 0:
                self.logger.error('Cannot get information about package %s, %s',
                                  package_path, '\n'.join((out, err)))
                errored_packages.add(package_path)
                continue
            signature_line = None
            for line in out.split('\n'):
                line = line.strip()
                if line.startswith('Signature'):
                    signature_line = line
                    break
            if not signature_line:
                self.logger.error('No information about package %s signature',
                                  package_path)
                continue
            signature_result = signature_regex.search(signature_line)
            if not signature_result:
                self.logger.error('Cannot detect information '
                                  'about package %s signature', package_path)
                errored_packages.add(package_path)
                continue
            pkg_key_id = signature_result.groupdict().get('key_id', '').lower()
            if 'none' in pkg_key_id:
                self.logger.error('Package %s is not signed', package_path)
                no_signature_packages.add(package_path)
            elif pkg_key_id not in key_ids_lower:
                # Check if package is signed with known sub-key
                signed_with_subkey = False
                for key, subkeys in self.known_subkeys.items():
                    if pkg_key_id in subkeys:
                        signed_with_subkey = True
                        break
                if not signed_with_subkey:
                    self.logger.error('Package %s is signed with wrong key, '
                                      'expected "%s", got "%s"',
                                      package_path, str(key_ids_lower),
                                      pkg_key_id)
                wrong_signature_packages.add(f'{package_path} {pkg_key_id}')

        if errored_packages or no_signature_packages or wrong_signature_packages:
            if not os.path.exists(self.export_error_file):
                mode = 'wt'
            else:
                mode = 'at'
            lines = []
            if errored_packages:
                lines.append('Packages that we cannot get information about:')
                lines.extend(list(errored_packages))
            if no_signature_packages:
                lines.append('Packages without signature:')
                lines.extend(list(no_signature_packages))
            if wrong_signature_packages:
                lines.append('Packages with wrong signature:')
                lines.extend(wrong_signature_packages)
            with open(self.export_error_file, mode=mode) as f:
                f.write('\n'.join(lines))

    async def export_repos_from_pulp(
        self,
        platform_names: typing.List[str] = None,
        repo_ids: typing.List[int] = None,
        arches: typing.List[str] = None,
    ) -> typing.Tuple[typing.List[str], typing.Dict[int, str]]:
        platforms_dict = {}
        msg, msg_values = (
            'Start exporting packages for following platforms:\n%s',
            platform_names,
        )
        if repo_ids:
            msg, msg_values = (
                'Start exporting packages for following repositories:\n%s',
                repo_ids,
            )
        self.logger.info(msg, msg_values)
        where_conditions = models.Platform.is_reference.is_(False)
        if platform_names is not None:
            where_conditions = sqlalchemy.and_(
                models.Platform.name.in_(platform_names),
                models.Platform.is_reference.is_(False),
            )
        query = select(models.Platform).where(
            where_conditions).options(
            selectinload(models.Platform.repos),
            selectinload(models.Platform.sign_keys)
        )
        async with database.Session() as db:
            db_platforms = await db.execute(query)
        db_platforms = db_platforms.scalars().all()

        repos_x86_64 = {}
        repos_ppc64le = {}
        final_export_paths = []
        for db_platform in db_platforms:
            repo_ids_to_export = []
            platforms_dict[db_platform.id] = []
            for repo in db_platform.repos:
                if (repo_ids is not None and repo.id not in repo_ids) or (
                        repo.production is False):
                    continue
                repo_name = self.get_full_repo_name(repo)
                if repo.arch == 'x86_64':
                    repos_x86_64[repo_name] = (repo.pulp_href, repo.debug)
                if repo.arch == 'ppc64le':
                    repos_ppc64le[repo_name] = (repo.pulp_href, repo.debug)
                if arches is not None:
                    if repo.arch in arches:
                        platforms_dict[db_platform.id].append(repo.export_path)
                        repo_ids_to_export.append(repo.id)
                else:
                    platforms_dict[db_platform.id].append(repo.export_path)
                    repo_ids_to_export.append(repo.id)
            exported_paths = await self.export_repositories(
                list(set(repo_ids_to_export)))
            final_export_paths.extend(exported_paths)
            for repo_path in exported_paths:
                if not os.path.exists(repo_path):
                    self.logger.error('Path %s does not exist', repo_path)
                    continue
                try:
                    local['sudo']['chown', '-R',
                                  f'{self.current_user}:{self.current_user}',
                                  f'{repo_path}'].run()
                    # removing files with partial modulemd data
                    local['find'][repo_path, '-type', 'f', '-name', '*snippet',
                                  '-exec', 'rm', '-f', '{}', '+'].run()
                    self.check_rpms_signature(repo_path, db_platform.sign_keys)
                finally:
                    local['sudo'][
                        'chown', '-R',
                        f'{self.pulp_system_user}:{self.pulp_system_user}',
                        f'{repo_path}'
                    ].run()
            self.logger.info('All repositories exported in following paths:\n%s',
                             '\n'.join((str(path) for path in exported_paths)))
        await self.prepare_and_execute_async_tasks(repos_x86_64, repos_ppc64le)
        return final_export_paths, platforms_dict

    async def export_repos_from_release(
        self,
        release_id: int,
    ) -> typing.Tuple[typing.List[str], int]:
        self.logger.info('Start exporting packages from release id=%s',
                         release_id)
        async with database.Session() as db:
            db_release = await db.execute(
                select(models.Release).where(models.Release.id == release_id))
        db_release = db_release.scalars().first()

        repo_ids = jmespath.search('packages[].repositories[].id',
                                   db_release.plan)
        repo_ids = list(set(repo_ids))
        async with database.Session() as db:
            db_repos = await db.execute(
                select(models.Repository).where(sqlalchemy.and_(
                    models.Repository.id.in_(repo_ids),
                    models.Repository.production.is_(True),
                ))
            )
        repos_x86_64 = {}
        repos_ppc64le = {}
        for db_repo in db_repos.scalars().all():
            repo_name = self.get_full_repo_name(db_repo)
            if db_repo.arch == 'x86_64':
                repos_x86_64[repo_name] = (db_repo.pulp_href, db_repo.debug)
            if db_repo.arch == 'ppc64le':
                repos_ppc64le[repo_name] = (db_repo.pulp_href, db_repo.debug)
        await self.prepare_and_execute_async_tasks(repos_x86_64, repos_ppc64le)
        exported_paths = await self.export_repositories(repo_ids)
        return exported_paths, db_release.platform_id

    async def delete_existing_exporters_from_pulp(self):
        deleted_exporters = []
        existing_exporters = await self.pulp_client.list_filesystem_exporters()
        for exporter in existing_exporters:
            await self.pulp_client.delete_filesystem_exporter(
                exporter['pulp_href'])
            deleted_exporters.append(exporter['name'])
        if deleted_exporters:
            self.logger.info(
                'Following exporters, has been deleted from pulp:\n%s',
                '\n'.join(str(i) for i in deleted_exporters),
            )

    def regenerate_repo_metadata(self, repo_path):
        _, stdout, _ = self.createrepo_c.run(
            args=['--update', '--keep-all-metadata', repo_path],
        )
        self.logger.info(stdout)

    def update_ppc64le_errata(self, repodata: Path):
        output_file = repodata / 'updateinfo.xml'
        input_repodata = Path(
            str(repodata).replace('ppc64le', 'x86_64')
        )
        input_updateinfo = list(input_repodata.glob('*updateinfo.xml*'))
        if input_updateinfo:
            input_updateinfo = input_updateinfo[0]
            update_updateinfo(
                str(input_updateinfo),
                str(repodata),
                str(output_file)
            )
            self.modifyrepo_c[
                '--mdtype=updateinfo', str(output_file), str(repodata)
            ].run()
            output_file.unlink()


def main():
    args = parse_args()

    platforms_dict = {}
    key_id_by_platform = None
    exported_paths = []
    pulp_client = PulpClient(
        settings.pulp_host,
        settings.pulp_user,
        settings.pulp_password,
    )
    exporter = Exporter(
        pulp_client=pulp_client,
        copy_noarch_packages=args.copy_noarch_packages,
        only_check_noarch=args.only_check_noarch,
        show_differ_packages=args.show_differ_packages,
    )
    if args.distribution:
        sync(exporter.check_noarch_in_user_distribution_repos(
            args.distribution))
        return
    sync(exporter.delete_existing_exporters_from_pulp())

    db_sign_keys = sync(exporter.get_sign_keys())
    if args.release_id:
        release_id = args.release_id
        exported_paths, platform_id = sync(exporter.export_repos_from_release(
            release_id))
        key_id_by_platform = next((
            sign_key['keyid'] for sign_key in db_sign_keys
            if sign_key['platform_id'] == platform_id
        ), None)

    if args.platform_names or args.repo_ids:
        platform_names = args.platform_names
        repo_ids = args.repo_ids
        exported_paths, platforms_dict = sync(exporter.export_repos_from_pulp(
            platform_names=platform_names,
            arches=args.arches,
            repo_ids=repo_ids,
        ))

    for exp_path in exported_paths:
        string_exp_path = str(exp_path)
        path = Path(exp_path)
        repo_path = path.parent
        repodata = repo_path / 'repodata'
        if not os.path.exists(repo_path):
            continue
        try:
            local['sudo']['chown', '-R',
                          f'{exporter.current_user}:{exporter.current_user}',
                          f'{repo_path}'].run()
            exporter.regenerate_repo_metadata(repo_path)
            key_id = key_id_by_platform or None
            for platform_id, platform_repos in platforms_dict.items():
                for repo_export_path in platform_repos:
                    if repo_export_path in string_exp_path:
                        key_id = next((
                            sign_key['keyid'] for sign_key in db_sign_keys
                            if sign_key['platform_id'] == platform_id
                        ), None)
                        break
            if 'ppc64le' in exp_path:
                exporter.update_ppc64le_errata(repodata)
            sync(exporter.repomd_signer(repodata, key_id))
        finally:
            local['sudo']['chown', '-R',
                          f'{exporter.pulp_system_user}:{exporter.pulp_system_user}',
                          f'{repo_path}'].run()


if __name__ == '__main__':
    main()
