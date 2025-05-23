import abc
import logging
import platform
import sys

from threading import RLock
from typing import Any, Callable, Dict, Optional, Union

from ..config import Config
from ..exceptions import WatchdogError

__all__ = ['WatchdogError', 'Watchdog']

logger = logging.getLogger(__name__)

MODE_REQUIRED = 'required'    # Will not run if a watchdog is not available
MODE_AUTOMATIC = 'automatic'  # Will use a watchdog if one is available
MODE_OFF = 'off'              # Will not try to use a watchdog


def parse_mode(mode: Union[bool, str]) -> str:
    if mode is False:
        return MODE_OFF
    mode = str(mode).lower()
    if mode in ['require', 'required']:
        return MODE_REQUIRED
    elif mode in ['auto', 'automatic']:
        return MODE_AUTOMATIC
    else:
        if mode not in ['off', 'disable', 'disabled']:
            logger.warning("Watchdog mode {0} not recognized, disabling watchdog".format(mode))
        return MODE_OFF


def synchronized(func: Callable[..., Any]) -> Callable[..., Any]:
    def wrapped(self: 'Watchdog', *args: Any, **kwargs: Any) -> Any:
        with self.lock:
            return func(self, *args, **kwargs)
    return wrapped


class WatchdogConfig(object):
    """Helper to contain a snapshot of configuration"""
    def __init__(self, config: Config) -> None:
        watchdog_config = config.get("watchdog") or {'mode': 'automatic'}

        self.mode = parse_mode(watchdog_config.get('mode', 'automatic'))
        self.ttl = config['ttl']
        self.loop_wait = config['loop_wait']
        self.safety_margin = watchdog_config.get('safety_margin', 5)
        self.driver = watchdog_config.get('driver', 'default')
        self.driver_config = dict((k, v) for k, v in watchdog_config.items()
                                  if k not in ['mode', 'safety_margin', 'driver'])

    def __eq__(self, other: Any) -> bool:
        return isinstance(other, WatchdogConfig) and \
            all(getattr(self, attr) == getattr(other, attr) for attr in
                ['mode', 'ttl', 'loop_wait', 'safety_margin', 'driver', 'driver_config'])

    def __ne__(self, other: Any) -> bool:
        return not self == other

    def get_impl(self) -> 'WatchdogBase':
        if self.driver == 'testing':  # pragma: no cover
            from patroni.watchdog.linux import TestingWatchdogDevice
            return TestingWatchdogDevice.from_config(self.driver_config)
        elif platform.system() == 'Linux' and self.driver == 'default':
            from patroni.watchdog.linux import LinuxWatchdogDevice
            return LinuxWatchdogDevice.from_config(self.driver_config)
        else:
            return NullWatchdog()

    @property
    def timeout(self) -> int:
        if self.safety_margin == -1:
            return int(self.ttl // 2)
        else:
            return self.ttl - self.safety_margin

    @property
    def timing_slack(self) -> int:
        return self.timeout - self.loop_wait


class Watchdog(object):
    """Facade to dynamically manage watchdog implementations and handle config changes.

    When activation fails underlying implementation will be switched to a Null implementation. To avoid log spam
    activation will only be retried when watchdog configuration is changed."""
    def __init__(self, config: Config) -> None:
        self.config = WatchdogConfig(config)
        self.active_config: WatchdogConfig = self.config
        self.lock = RLock()
        self.active = False

        if self.config.mode == MODE_OFF:
            self.impl = NullWatchdog()
        else:
            self.impl = self.config.get_impl()
            if self.config.mode == MODE_REQUIRED and self.impl.is_null:
                logger.error("Configuration requires a watchdog, but watchdog is not supported on this platform.")
                sys.exit(1)

    @synchronized
    def reload_config(self, config: Config) -> None:
        self.config = WatchdogConfig(config)
        # Turning a watchdog off can always be done immediately
        if self.config.mode == MODE_OFF:
            if self.active:
                self._disable()
            self.active_config = self.config
            self.impl = NullWatchdog()
        # If watchdog is not active we can apply config immediately to show any warnings early. Otherwise we need to
        # delay until next time a keepalive is sent so timeout matches up with leader key update.
        if not self.active:
            if self.config.driver != self.active_config.driver or \
               self.config.driver_config != self.active_config.driver_config:
                self.impl = self.config.get_impl()
            self.active_config = self.config

    @synchronized
    def activate(self) -> bool:
        """Activates the watchdog device with suitable timeouts. While watchdog is active keepalive needs
        to be called every time loop_wait expires.

        :returns False if a safe watchdog could not be configured, but is required.
        """
        self.active = True
        return self._activate()

    def _activate(self) -> bool:
        self.active_config = self.config

        if self.config.timing_slack < 0:
            logger.warning('Watchdog not supported because leader TTL {0} is less than 2x loop_wait {1}'
                           .format(self.config.ttl, self.config.loop_wait))
            self.impl = NullWatchdog()

        try:
            self.impl.open()
            actual_timeout = self._set_timeout()
        except WatchdogError as e:
            log = logger.warning if self.config.mode == MODE_REQUIRED else logger.debug
            log("Could not activate %s: %s", self.impl.describe(), e)
            self.impl = NullWatchdog()
            actual_timeout = self.impl.get_timeout()

        if self.impl.is_running and not self.impl.can_be_disabled:
            logger.warning("Watchdog implementation can't be disabled."
                           " Watchdog will trigger after Patroni loses leader key.")

        if not self.impl.is_running or actual_timeout and actual_timeout > self.config.timeout:
            if self.config.mode == MODE_REQUIRED:
                if self.impl.is_null:
                    logger.error("Configuration requires watchdog, but watchdog could not be configured.")
                else:
                    logger.error("Configuration requires watchdog, but a safe watchdog timeout {0} could"
                                 " not be configured. Watchdog timeout is {1}.".format(
                                     self.config.timeout, actual_timeout))
                return False
            else:
                if not self.impl.is_null:
                    logger.warning("Watchdog timeout {0} seconds does not ensure safe termination within {1} seconds"
                                   .format(actual_timeout, self.config.timeout))

        if self.is_running:
            logger.info("{0} activated with {1} second timeout, timing slack {2} seconds"
                        .format(self.impl.describe(), actual_timeout, self.config.timing_slack))
        else:
            if self.config.mode == MODE_REQUIRED:
                logger.error("Configuration requires watchdog, but watchdog could not be activated")
                return False

        return True

    def _set_timeout(self) -> Optional[int]:
        if self.impl.has_set_timeout():
            self.impl.set_timeout(self.config.timeout)

        # Safety checks for watchdog implementations that don't support configurable timeouts
        actual_timeout = self.impl.get_timeout()
        if self.impl.is_running and actual_timeout < self.config.loop_wait:
            logger.error('loop_wait of {0} seconds is too long for watchdog {1} second timeout'
                         .format(self.config.loop_wait, actual_timeout))
            if self.impl.can_be_disabled:
                logger.info('Disabling watchdog due to unsafe timeout.')
                self.impl.close()
                self.impl = NullWatchdog()
                return None
        return actual_timeout

    @synchronized
    def disable(self) -> None:
        self._disable()
        self.active = False

    def _disable(self) -> None:
        try:
            if self.impl.is_running and not self.impl.can_be_disabled:
                # Give sysadmin some extra time to clean stuff up.
                self.impl.keepalive()
                logger.warning("Watchdog implementation can't be disabled. System will reboot after "
                               "{0} seconds when watchdog times out.".format(self.impl.get_timeout()))
            self.impl.close()
        except WatchdogError as e:
            logger.error("Error while disabling watchdog: %s", e)

    @synchronized
    def keepalive(self) -> None:
        try:
            if self.active:
                self.impl.keepalive()
            # In case there are any pending configuration changes apply them now.
            if self.active and self.config != self.active_config:
                if self.config.mode != MODE_OFF and self.active_config.mode == MODE_OFF:
                    self.impl = self.config.get_impl()
                    self._activate()
                if self.config.driver != self.active_config.driver \
                   or self.config.driver_config != self.active_config.driver_config:
                    self._disable()
                    self.impl = self.config.get_impl()
                    self._activate()
                if self.config.timeout != self.active_config.timeout:
                    self.impl.set_timeout(self.config.timeout)
                    if self.is_running:
                        logger.info("{0} updated with {1} second timeout, timing slack {2} seconds"
                                    .format(self.impl.describe(), self.impl.get_timeout(), self.config.timing_slack))
                self.active_config = self.config
        except WatchdogError as e:
            logger.error("Error while sending keepalive: %s", e)

    @property
    @synchronized
    def is_running(self) -> bool:
        return self.impl.is_running

    @property
    @synchronized
    def is_healthy(self) -> bool:
        if self.config.mode != MODE_REQUIRED:
            return True
        return self.config.timing_slack >= 0 and self.impl.is_healthy


class WatchdogBase(abc.ABC):
    """A watchdog object when opened requires periodic calls to keepalive.
    When keepalive is not called within a timeout the system will be terminated."""
    is_null = False

    @property
    def is_running(self) -> bool:
        """Returns True when watchdog is activated and capable of performing it's task."""
        return False

    @property
    def is_healthy(self) -> bool:
        """Returns False when calling open() is known to fail."""
        return False

    @property
    def can_be_disabled(self) -> bool:
        """Returns True when watchdog will be disabled by calling close(). Some watchdog devices
        will keep running no matter what once activated. May raise WatchdogError if called without
        calling open() first."""
        return True

    @abc.abstractmethod
    def open(self) -> None:
        """Open watchdog device.

        When watchdog is opened keepalive must be called. Returns nothing on success
        or raises WatchdogError if the device could not be opened."""

    @abc.abstractmethod
    def close(self) -> None:
        """Gracefully close watchdog device."""

    @abc.abstractmethod
    def keepalive(self) -> None:
        """Resets the watchdog timer.

        Watchdog must be open when keepalive is called."""

    @abc.abstractmethod
    def get_timeout(self) -> int:
        """Returns the current keepalive timeout in effect."""

    def has_set_timeout(self) -> bool:
        """Returns True if setting a timeout is supported."""
        return False

    def set_timeout(self, timeout: int) -> None:
        """Set the watchdog timer timeout.

        :param timeout: watchdog timeout in seconds"""
        raise WatchdogError("Setting timeout is not supported on {0}".format(self.describe()))

    def describe(self) -> str:
        """Human readable name for this device"""
        return self.__class__.__name__

    @classmethod
    def from_config(cls, config: Dict[str, Any]) -> 'WatchdogBase':
        return cls()


class NullWatchdog(WatchdogBase):
    """Null implementation when watchdog is not supported."""
    is_null = True

    def open(self) -> None:
        return

    def close(self) -> None:
        return

    def keepalive(self) -> None:
        return

    def get_timeout(self) -> int:
        # A big enough number to not matter
        return 1000000000
