import logging
from functools import cached_property
from pathlib import Path
from urllib.parse import urljoin
from urllib.parse import urlunsplit

from broker.hosts import Host

from robottelo.config import settings
from robottelo.constants import DISTRO_RHEL6
from robottelo.constants import DISTRO_RHEL7
from robottelo.constants import REPOS
from robottelo.helpers import install_katello_ca
from robottelo.helpers import remove_katello_ca

logger = logging.getLogger('robottelo')


def setup_capsule(satellite, capsule, registration_args=None, installation_args=None):
    """Given satellite and capsule instances, run the commands needed to set up the capsule

    Note: This does not perform content setup actions on the Satellite

    :param satellite: An instance of this module's Satellite class
    :param capsule: An instance of this module's Capsule class
    :param registration_args: A dictionary mapping argument: value pairs for registration
    :param installation_args: A dictionary mapping argument: value pairs for installation
    :return: An ssh2-python result object for the installation command.

    """
    if not registration_args:
        registration_args = {}
    file, cmd_args = satellite.capsule_certs_generate(capsule)
    if installation_args:
        cmd_args.update(installation_args)
    satellite.execute(
        f'sshpass -p "{capsule.password}" scp -o "StrictHostKeyChecking no" '
        f'{file} root@{capsule.hostname}:{file}'
    )
    capsule.install_katello_ca(sat_hostname=satellite.hostname)
    capsule.register_contenthost(**registration_args)
    return capsule.install(**cmd_args)


class ContentHostError(Exception):
    pass


class ContentHost(Host):
    run = Host.execute

    def download_install_rpm(self, repo_url, package_name):
        """Downloads and installs custom rpm on the broker virtual machine.

        :param repo_url: URL to repository, where package is located.
        :param package_name: Desired package name.
        :return: None.
        :raises robottelo.hosts.ContentHostError: If package wasn't installed.

        """
        self.execute(f'wget -nd -r -l1 --no-parent -A \'{package_name}.rpm\' {repo_url}')
        self.execute(f'rpm -i {package_name}.rpm')
        result = self.execute(f'rpm -q {package_name}')
        if not result.status:
            raise ContentHostError(f'Failed to install {package_name} rpm.')

    def enable_repo(self, repo, force=False):
        """Enables specified Red Hat repository on the broker virtual machine.
        Does nothing if downstream capsule or satellite tools repo was passed.
        Custom repos are enabled by default when registering a host.

        :param repo: Red Hat repository name.
        :param force: enforce enabling command, even when custom repos are
            detected for satellite tools or capsule.
        :return: None.

        """
        downstream_repo = None
        if repo == REPOS['rhst6']['id']:
            downstream_repo = settings.sattools_repo['rhel6']
        elif repo == REPOS['rhst7']['id']:
            downstream_repo = settings.sattools_repo['rhel7']
        elif repo in (REPOS['rhsc6']['id'], REPOS['rhsc7']['id']):
            downstream_repo = settings.capsule_repo
        if force or settings.cdn or not downstream_repo:
            self.execute(f'subscription-manager repos --enable {repo}')

    def subscription_manager_list_repos(self):
        return self.execute("subscription-manager repos --list")

    def subscription_manager_status(self):
        return self.execute("subscription-manager status")

    def subscription_manager_list(self):
        return self.execute("subscription-manager list")

    def create_custom_repos(self, **kwargs):
        """Create custom repofiles.
        Each ``kwargs`` item will result in one repository file created. Where
        the key is the repository filename and repository name, and the value
        is the repository URL.

        For example::

            create_custom_repo(custom_repo='http://repourl.domain.com/path')

        Will create a repository file named ``custom_repo.repo`` with
        the following contents::

            [custom_repo]
            name=custom_repo
            baseurl=http://repourl.domain.com/path
            enabled=1
            gpgcheck=0

        """
        for name, url in kwargs.items():
            content = f'[{name}]\n' f'name={name}\n' f'baseurl={url}\n' 'enabled=1\n' 'gpgcheck=0'
            self.execute(f'echo "{content}" > /etc/yum.repos.d/{name}.repo')

    def install_katello_host_tools(self):
        """Installs Katello host tools on the broker virtual machine

        :raises robottelo.hosts.ContentHostError: If katello-host-tools wasn't
            installed.
        """
        self.execute('yum install -y katello-host-tools')
        result = self.execute('rpm -q katello-host-tools')
        if not result.status:
            raise ContentHostError('Failed to install katello-host-tools')

    def install_katello_ca(self, sat_hostname=None):
        """Downloads and installs katello-ca rpm on the broker virtual machine.

        Uses common helper `install_katello_ca(hostname=None)`, but passes
        `self.hostname` instead of the hostname as we are using fake hostnames
        for broker virtual machines.

        :return: None.
        :raises robottelo.hosts.ContentHostError: If katello-ca wasn't
            installed.
        """
        try:
            install_katello_ca(hostname=self.hostname, sat_hostname=sat_hostname)
        except AssertionError:
            raise ContentHostError('Failed to download and install the katello-ca rpm')

    def install_capsule_katello_ca(self, capsule=None):
        """Downloads and installs katello-ca rpm on the broker virtual machine.

        :param: str capsule: Capsule hostname
        :raises robottelo.hosts.ContentHostError: If katello-ca wasn't
            installed.
        """
        url = urlunsplit(('http', capsule, 'pub/', '', ''))
        ca_url = urljoin(url, 'katello-ca-consumer-latest.noarch.rpm')
        self.execute(f'rpm -Uvh {ca_url}')
        result = self.execute(f'rpm -q katello-ca-consumer-{capsule}')
        if not result.status:
            raise ContentHostError('Failed to install the katello-ca rpm')

    def register_contenthost(
        self,
        org="Default_Organization",
        activation_key=None,
        lce="Library",
        consumerid=None,
        force=True,
        releasever=None,
        username=None,
        password=None,
        auto_attach=False,
    ):
        """Registers content host on foreman server using activation-key. This
        can be done in two ways: either by specifying organization name and
        activation key name or by specifying organization name and lifecycle
        environment name (administrator credentials for authentication will be
        passed automatically)

        :param activation_key: Activation key name to register content host
            with.
        :param lce: lifecycle environment name to which register the content
            host.
        :param consumerid: uuid of content host, register to this content host,
            content host has to be created before
        :param org: Organization name to register content host for.
        :param force: Register the content host even if it's already registered
        :param releasever: Set a release version
        :param username: a user name to register the content host with
        :param password: the user password
        :param auto_attach: automatically attach compatible subscriptions to
            this system.
        :return: SSHCommandResult instance filled with the result of the
            registration.
        """
        cmd = f'subscription-manager register --org {org}'
        if activation_key is not None:
            cmd += f' --activationkey {activation_key}'
        elif lce:
            if username is None and password is None:
                username = settings.server.admin_username
                password = settings.server.admin_password

            cmd += f' --environment {lce} --username {username} --password {password}'
            if auto_attach:
                cmd += ' --auto-attach'
        elif consumerid:
            if username is None and password is None:
                username = settings.server.admin_username
                password = settings.server.admin_password

            cmd += f' --consumerid {consumerid} --username {username} --password {password}'
            if auto_attach:
                cmd += ' --auto-attach'
        else:
            raise ContentHostError(
                'Please provide either activation key or lifecycle '
                'environment name to successfully register a host'
            )
        if releasever is not None:
            cmd += f' --release {releasever}'
        if force:
            cmd += ' --force'
        result = self.execute(cmd)
        if 'The system has been registered with ID' in ''.join(result.stdout):
            self.subscribed = True
        return result

    def remove_katello_ca(self):
        """Removes katello-ca rpm from the broker virtual machine.

        :return: None.
        :raises robottelo.hosts.ContentHostError: If katello-ca wasn't removed.
        """
        try:
            remove_katello_ca(hostname=self.hostname)
        except AssertionError:
            raise ContentHostError('Failed to remove the katello-ca rpm')

    def remove_capsule_katello_ca(self, capsule=None):
        """Removes katello-ca rpm and reset rhsm.conf from the broker virtual machine.

        :param: str capsule: Capsule hostname
        :raises robottelo.hosts.ContentHostError: If katello-ca wasn't removed.
        """
        self.execute('yum erase -y $(rpm -qa |grep katello-ca-consumer)')
        result = self.execute(f'rpm -q katello-ca-consumer-{capsule}')
        if result.status == 0:
            raise ContentHostError('Failed to remove the katello-ca rpm')
        rhsm_updates = [
            's/^hostname.*/hostname=subscription.rhn.redhat.com/',
            's|^prefix.*|prefix=/subscription|',
            's|^baseurl.*|baseurl=https://cdn.redhat.com|',
            's/^repo_ca_cert.*/repo_ca_cert=%(ca_cert_dir)sredhat-uep.pem/',
        ]
        for command in rhsm_updates:
            result = self.execute(f'sed -i -e "{command}" /etc/rhsm/rhsm.conf')
            if not result.status:
                raise ContentHostError('Failed to reset the rhsm.conf')

    def unregister(self):
        """Run subscription-manager unregister.

        :return: SSHCommandResult instance filled with the result of the
            unregistration.

        """
        return self.execute('subscription-manager unregister')

    def get(self, remote_path, local_path=None):
        """Get a remote file from the broker virtual machine."""
        self.session.sftp_read(source=remote_path, destination=local_path)

    def put(self, local_path, remote_path=None):
        """Put a local file to the broker virtual machine."""
        self.session.sftp_write(source=local_path, destination=remote_path)

    def configure_rhel_repo(self, rhel_repo):
        """Configures specified Red Hat repository on the broker virtual machine.

        :param rhel_repo: Red Hat repository link from properties file.
        :return: None.

        """
        # 'Access Insights', 'puppet' requires RHEL 6/7 repo and it is not
        # possible to sync the repo during the tests as they are huge(in GB's)
        # hence this adds a file in /etc/yum.repos.d/rhel6/7.repo
        self.execute(f'curl -O /etc/yum.repos.d/rhel.repo {rhel_repo}')

    def execute_foreman_scap_client(self, policy_id=None):
        """Executes foreman_scap_client on the vm to create security audit report.

        :param policy_id: The Id of the OSCAP policy.
        :return: None.

        """
        if policy_id is None:
            result = self.execute(
                'awk -F "/" \'/download_path/ {print $4}\' /etc/foreman_scap_client/config.yaml'
            )
            policy_id = result.stdout[0]
        result = self.execute(f'foreman_scap_client {policy_id}')
        if not result.status:
            raise ContentHostError('Failed to execute foreman_scap_client run.')

    def configure_rhai_client(self, activation_key, org, rhel_distro):
        """Configures a Red Hat Access Insights service on the system by
        installing the redhat-access-insights package and registering to the
        service.

        :param activation_key: Activation key to be used to register the
            system to satellite
        :param org: The org to which the system is required to be registered
        :param rhel_distro: rhel distribution used by the vm
        :return: None
        """
        # Download and Install ketello-ca rpm
        self.install_katello_ca()
        self.register_contenthost(org, activation_key)

        # Red Hat Access Insights requires RHEL 6 or 7 repo and it is not
        # possible to sync the repo during the tests; therefore, adding repo file.
        if rhel_distro == DISTRO_RHEL6:
            rhel_repo = settings.rhel6_repo
            insights_repo = settings.rhai.insights_client_el6repo
        if rhel_distro == DISTRO_RHEL7:
            rhel_repo = settings.rhel7_repo
            insights_repo = settings.rhai.insights_client_el7repo

        missing_repos = []
        if insights_repo is None:
            missing_repos.append('RHAI client')
        if rhel_repo is None:
            missing_repos.append('RHEL')
        if missing_repos:
            raise ContentHostError(
                f'Missing {" and ".join(missing_repos)} '
                f'repository configuration for {rhel_distro}.'
            )

        self.configure_rhel_repo(rhel_repo)

        self.execute(f'curl -O /etc/yum.repos.d/insights.repo {insights_repo}')

        # Install redhat-access-insights package
        package_name = 'insights-client'
        result = self.execute(f'yum install -y {package_name}')
        if not result.status:
            raise ContentHostError('Unable to install redhat-access-insights package')

        # Verify if package is installed by rpm query
        result = self.execute(f'rpm -qi {package_name}')
        logger.info(f'Insights client rpm version: {result.stdout}')
        if not result.status:
            raise ContentHostError('Unable to install redhat-access-insights package')

        # Register client with Red Hat Access Insights
        result = self.execute('insights-client --register')
        if not result.status:
            raise ContentHostError(
                'Unable to register client to Access Insights through Satellite'
            )

    def set_infrastructure_type(self, infrastructure_type="physical"):
        """Force host to appear as bare-metal orbroker virtual machine in
        subscription-manager fact.

        :param str infrastructure_type: One of "physical", "virtual"
        """
        script_path = "/usr/sbin/virt-what"
        self.execute(f"cp -n {script_path} {script_path}.old")

        script_content = ["#!/bin/sh -"]
        if infrastructure_type == "virtual":
            script_content.append("echo kvm")
        script_content = "\n".join(script_content)
        self.execute(f"echo -e '{script_content}' > {script_path}")

    def patch_os_release_version(self, distro=DISTRO_RHEL7):
        """Patch VM OS release version.

        This is needed by yum package manager to generate the right RH
        repositories urls.
        """
        if distro == DISTRO_RHEL7:
            rh_product_os_releasever = REPOS['rhel7']['releasever']
        else:
            raise ContentHostError('No distro package available to retrieve release version')
        return self.execute(f"echo '{rh_product_os_releasever}' > /etc/yum/vars/releasever")


class Capsule(ContentHost):
    def restart_services(self):
        """Restart services, returning True if passed and stdout if not"""
        result = self.execute('foreman-maintain service restart').status
        return True if result.status == 0 else result.stdout

    def check_services(self):
        error_msg = 'Some services are not running'
        result = self.execute('foreman-maintain service status')
        if result.status == 0:
            return True
        for line in result.stdout.splitlines():
            if error_msg in line:
                return line.replace(error_msg, '').strip()

    def install(self, **cmd_kwargs):
        """General purpose installer"""
        command_args = {"scenario": self.__class__.__name__.lower()}
        command_args.update(cmd_kwargs)
        command_args = ' '.join(
            [f'--{key.replace("_", "-")} {value}' for key, value in command_args.items()]
        )
        self.execute(f'echo "satellite-installer {command_args}" > /root/install.txt')
        return self.execute(f'satellite-installer {command_args}')


class Satellite(Capsule):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._init_nailgun()
        self._init_cli()
        self._init_airgun()

    def _init_nailgun(self):
        """Import all nailgun entities and wrap them under self.api"""
        from nailgun.config import ServerConfig
        from nailgun.entity_mixins import Entity
        from nailgun import entities

        def inject_config(cls, server_config):
            """inject a nailgun server config into the init of nailgun entity classes"""
            import functools

            class DecClass(cls):
                __init__ = functools.partialmethod(cls.__init__, server_config=server_config)

            return DecClass

        # set the server configuration to point to this satellite
        self.nailgun_cfg = ServerConfig(
            auth=(settings.server.admin_username, settings.server.admin_password),
            url=f'https://{self.hostname}',
            verify=False,
        )
        # add each nailgun entity to self.api, injecting our server config
        self.api = lambda: None
        for name, obj in entities.__dict__.items():
            try:
                if Entity in obj.mro():
                    setattr(self.api, name, inject_config(obj, self.nailgun_cfg))
            except AttributeError:
                # not everything has an mro method, we don't care about them
                pass

    def _init_cli(self):
        """Import all robottelo cli entities and wrap them under self.cli"""
        import importlib
        from robottelo.cli.base import Base

        self.cli = lambda: None
        for file in Path('robottelo/cli/').iterdir():
            if file.suffix == '.py' and not file.name.startswith('_'):
                cli_module = importlib.import_module(f'robottelo.cli.{file.stem}')
                for name, obj in cli_module.__dict__.items():
                    try:
                        if Base in obj.mro():
                            # set our hostname as a class attribute
                            obj.hostname = self.hostname
                            setattr(self.cli, name, obj)
                    except AttributeError:
                        # not everything has an mro method, we don't care about them
                        pass

    def _init_airgun(self):
        """Initialize an airgun Session object and store it as self.ui_session"""
        from airgun.session import Session

        def get_caller():
            import inspect

            for frame in inspect.stack():
                if frame.function.startswith('test_'):
                    return frame.function

        self.ui_session = Session(
            session_name=get_caller(),
            user=settings.server.admin_username,
            password=settings.server.admin_password,
            hostname=self.hostname,
        )

    @cached_property
    def version(self):
        return self.execute('rpm -q satellite').stdout.split('-')[1]

    def capsule_certs_generate(self, capsule, **extra_kwargs):
        """Generate capsule certs, returning the cert path and the installer command args"""
        command = (
            f'capsule-certs-generate --foreman-proxy-fqdn {capsule.hostname} '
            f'--certs-tar /root/{capsule.hostname}-certs.tar'
        )
        extras = ' '.join(
            [f'--{key.replace("_", "-")} {value}' for key, value in extra_kwargs.items()]
        )
        result = self.execute(f'{command} {extras}')
        installer_command, listening = '', False
        for line in result.stdout.splitlines():
            if line.strip().startswith('satellite-installer'):
                listening = True
            if listening:
                installer_command += ' ' + ' '.join(line.replace('\\', '').split())
        installer_command = installer_command.replace("satellite-installer", "").strip()
        cmd_args = {
            k: v for k, v in [s.strip().split() for s in installer_command.split("--") if s]
        }
        # capsule-certs-generate color codes this field which causes path recognition issues
        cmd_args['certs-tar-file'] = f'/root/{capsule.hostname}-certs.tar'
        return f'/root/{capsule.hostname}-certs.tar', cmd_args
