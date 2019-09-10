"""SysConfig helper module.

Manage grub, systemd, coufrequtils and kernel version configuration.
"""

import os
import subprocess
from datetime import datetime, timedelta, timezone

from charmhelpers.core import hookenv, host, unitdata
from charmhelpers.core.templating import render
from charmhelpers.fetch import apt_install, apt_update

GRUB_DEFAULT = 'Advanced options for Ubuntu>Ubuntu, with Linux {}'
CPUFREQUTILS_TMPL = 'cpufrequtils.j2'
GRUB_CONF_TMPL = 'grub.j2'
SYSTEMD_SYSTEM_TMPL = 'etc.systemd.system.conf.j2'

CPUFREQUTILS = '/etc/default/cpufrequtils'
GRUB_CONF = '/etc/default/grub.d/90-sysconfig.cfg'
SYSTEMD_SYSTEM = '/etc/systemd/system.conf'
KERNEL = 'kernel'


def parse_config_flags(config_flags):
    """Parse config flags into a dict.

    :param config_flags: key pairs list. Format: key1=value1,key2=value2
    :return dict: format {'key1': 'value1', 'key2': 'value2'}
    """
    config_flags = config_flags.replace(" ", "")
    key_value_pairs = config_flags.split(",")
    parsed_config_flags = {}
    for pair in key_value_pairs:
        if '=' in pair and len(pair.split('=', 1)) == 2:
            parsed_config_flags[pair.split("=")[0]] = pair.split("=")[1]
    return parsed_config_flags


def running_kernel():
    """Return kernel version running in the principal unit."""
    return os.uname().release


def boot_time():
    """Return timestamp of last boot."""
    with open('/proc/uptime', 'r') as f:
        uptime_seconds = float(f.readline().split()[0])
        boot_time = datetime.now(timezone.utc) - timedelta(seconds=uptime_seconds)
        return boot_time


class BootResourceState:
    """A class to track resources changed since last reboot."""

    def __init__(self, db=None):
        """Initialize empty db used to track resources updates."""
        if db is None:
            db = unitdata.kv()
        self.db = db

    def key_for(self, resource_name):
        """Return db key for a given resource."""
        return "sysconfig.boot_resource.{}".format(resource_name)

    def set_resource(self, resource_name):
        """Update db entry for the resource_name with time.now."""
        timestamp = datetime.now(timezone.utc)
        self.db.set(self.key_for(resource_name), timestamp.timestamp())

    def get_resource_changed_timestamp(self, resource_name):
        """Retrieve timestamp of last resource change recorded.

        :param resource_name: resource to check
        :return: datetime of resource change, or datetime.min if resource not registered
        """
        tfloat = self.db.get(self.key_for(resource_name))
        if tfloat is not None:
            return datetime.fromtimestamp(tfloat, timezone.utc)
        return datetime.min.replace(tzinfo=timezone.utc)  # We don't have a ts -> changed at dawn of time

    def resources_changed_since_boot(self, resource_names):
        """Given a list of resource names return those that have changed since boot.

        :param resource_names: list of names
        :return: list of names
        """
        boot_ts = boot_time()
        changed = [name for name in resource_names if boot_ts < self.get_resource_changed_timestamp(name)]
        return changed


class SysConfigHelper:
    """Update sysconfig, grub, kernel and cpufrequtils config."""

    boot_resources = BootResourceState()

    def __init__(self):
        """Retrieve charm configuration."""
        self.charm_config = hookenv.config()

    @property
    def enable_container(self):
        """Return enable-container config."""
        return self.charm_config['enable-container']

    @property
    def reservation(self):
        """Return reservation config."""
        return self.charm_config['reservation']

    @property
    def cpu_range(self):
        """Return cpu-range config."""
        return self.charm_config['cpu-range']

    @property
    def hugepages(self):
        """Return hugepages config."""
        return self.charm_config['hugepages']

    @property
    def hugepagesz(self):
        """Return hugepagesz config."""
        return self.charm_config['hugepagesz']

    @property
    def raid_autodetection(self):
        """Return raid-autodetection config option."""
        return self.charm_config['raid-autodetection']

    @property
    def enable_pti(self):
        """Return raid-autodetection config option."""
        return self.charm_config['enable-pti']

    @property
    def enable_iommu(self):
        """Return enable-iommu config option."""
        return self.charm_config['enable-iommu']

    @property
    def grub_config_flags(self):
        """Return grub-config-flags config option."""
        return parse_config_flags(self.charm_config['grub-config-flags'])

    @property
    def systemd_config_flags(self):
        """Return grub-config-flags config option."""
        return parse_config_flags(self.charm_config['systemd-config-flags'])

    @property
    def kernel_version(self):
        """Return grub-config-flags config option."""
        return self.charm_config['kernel-version']

    @property
    def update_grub(self):
        """Return grub-config-flags config option."""
        return self.charm_config['update-grub']

    @property
    def governor(self):
        """Return grub-config-flags config option."""
        return self.charm_config['governor']

    def _render_boot_resource(self, source, target, context):
        """Render the template and set the resource as changed."""
        render(source=source, templates_dir='templates', target=target, context=context)
        self.boot_resources.set_resource(target)

    def _is_kernel_already_running(self):
        """Check if the kernel version required by charm config is equal to kernel running."""
        configured = self.kernel_version
        if configured == running_kernel():
            hookenv.log("Already running kernel: {}".format(configured),  hookenv.DEBUG)
            return True
        return False

    def _update_grub(self):
        """Call update-grub when update-grub config param is set to True."""
        if self.update_grub and not host.is_container():
            subprocess.check_call(['/usr/sbin/update-grub'])
            hookenv.log('Running update-grub to apply grub conf updates',  hookenv.DEBUG)

    def is_config_valid(self):
        """Validate config parameters."""
        valid = True

        if self.reservation not in ['off', 'isolcpus', 'affinity']:
            hookenv.log('reservation not valid. Possible values: ["off", "isolcpus", "affinity"]',  hookenv.DEBUG)
            valid = False

        if self.raid_autodetection not in ['', 'noautodetect', 'partitionable']:
            hookenv.log('raid-autodetection not valid. '
                        'Possible values: ["off", "noautodetect", "partitionable"]',  hookenv.DEBUG)
            valid = False

        if self.governor not in ['', 'powersave', 'performance']:
            hookenv.log('governor not valid. Possible values: ["", "powersave", "performance"]',  hookenv.DEBUG)
            valid = False

        return valid

    def update_grub_file(self):
        """Update /etc/default/grub.d/90-sysconfig.cfg according to charm configuration.

        Will call update-grub if update-grub config is set to True.
        """
        context = {}
        if self.reservation == 'isolcpus':
            context['cpu_range'] = self.cpu_range
        if self.hugepages:
            context['hugepages'] = self.hugepages
        if self.hugepagesz:
            context['hugepagesz'] = self.hugepagesz
        if self.raid_autodetection:
            context['raid'] = self.raid_autodetection
        if not self.enable_pti:
            context['pti_off'] = True
        if self.enable_iommu:
            context['iommu'] = True

        context['grub_config_flags'] = self.grub_config_flags

        if self.kernel_version and not self._is_kernel_already_running():
            context['grub_default'] = GRUB_DEFAULT.format(self.kernel_version)

        self._render_boot_resource(GRUB_CONF_TMPL, GRUB_CONF, context)
        hookenv.log('grub configuration updated')
        self._update_grub()

    def update_systemd_system_file(self):
        """Update /etc/systemd/system.conf according to charm configuration."""
        context = {}
        if self.reservation == 'affinity':
            context['cpu_range'] = self.cpu_range
        context['systemd_config_flags'] = self.systemd_config_flags

        self._render_boot_resource(SYSTEMD_SYSTEM_TMPL, SYSTEMD_SYSTEM, context)
        hookenv.log('systemd configuration updated')

    def install_configured_kernel(self):
        """Install kernel as given by the kernel-version config option.

        Will install kernel and matching modules-extra package
        """
        if not self.kernel_version or self._is_kernel_already_running():
            hookenv.log('kernel running already to the reuired version',  hookenv.DEBUG)
            return

        configured = self.kernel_version
        pkgs = [tmpl.format(configured) for tmpl in ["linux-image-{}", "linux-modules-extra-{}"]]
        apt_update()
        apt_install(pkgs)
        hookenv.log("installing: {}".format(pkgs))
        self.boot_resources.set_resource(KERNEL)

    def update_cpufreq(self):
        """Update /etc/default/cpufrequtils and restart cpufrequtils service."""
        if self.governor not in ('', 'performance', 'powersave'):
            return
        context = {'governor': self.governor}
        self._render_boot_resource(CPUFREQUTILS_TMPL, CPUFREQUTILS, context)
        # Ensure the ondemand initscript is disabled if governor is set, lp#1822774 and lp#740127
        # Ondemand init script is not updated during test if host is container.
        if host.get_distrib_codename() == 'xenial' and not host.is_container():
            hookenv.log('disabling the ondemand initscript for lp#1822774'
                        ' and lp#740127 if a governor is specified',  hookenv.DEBUG)
            if self.governor:
                subprocess.check_call(
                        ['/usr/sbin/update-rc.d', '-f', 'ondemand', 'remove', '>', '/dev/null', '2>&1']
                )
            else:
                # Renable ondemand when governor is unset.
                subprocess.check_call(
                    ['/usr/sbin/update-rc.d', '-f', 'ondemand', 'defaults', '>', '/dev/null', '2>&1']
                )

        host.service_restart('cpufrequtils')

    def remove_grub_configuration(self):
        """Remove /etc/default/grub.d/90-sysconfig.cfg if exists.

        Will call update-grub if update-grub config is set to True.
        """
        grub_configuration_path = GRUB_CONF
        if not os.path.exists(grub_configuration_path):
            return
        os.remove(grub_configuration_path)
        hookenv.log(
            'deleted grub configuration at '.format(grub_configuration_path),
            hookenv.DEBUG
        )
        self._update_grub()
        self.boot_resources.set_resource(GRUB_CONF)

    def remove_systemd_configuration(self):
        """Remove systemd configuration.

        Will render systemd config with empty context.
        """
        context = {}
        self._render_boot_resource(SYSTEMD_SYSTEM_TMPL, SYSTEMD_SYSTEM, context)
        hookenv.log(
            'deleted systemd configuration at '.format(SYSTEMD_SYSTEM),
            hookenv.DEBUG
        )

    def remove_cpufreq_configuration(self):
        """Remove cpufrequtils configuration.

        Will render cpufrequtils config with empty context.
        """
        context = {}
        if host.get_distrib_codename() == 'xenial' and not host.is_container():
            hookenv.log('Enabling the ondemand initscript for lp#1822774'
                        ' and lp#740127', 'DEBUG')
            subprocess.check_call(
                    ['/usr/sbin/update-rc.d', '-f', 'ondemand', 'defaults', '>', '/dev/null', '2>&1']
            )

        self._render_boot_resource(CPUFREQUTILS_TMPL, CPUFREQUTILS, context)
        hookenv.log(
            'deleted cpufreq configuration at '.format(CPUFREQUTILS),
            hookenv.DEBUG
        )
        host.service_restart('cpufrequtils')