"""
Thrash mds by simulating failures
"""
import logging
import contextlib
import ceph_manager
import random
import time

from gevent.greenlet import Greenlet
from gevent.event import Event
from teuthology import misc as teuthology

from tasks.cephfs.filesystem import MDSCluster, Filesystem

log = logging.getLogger(__name__)


class MDSThrasher(Greenlet):
    """
    MDSThrasher::

    The MDSThrasher thrashes MDSs during execution of other tasks (workunits, etc).

    The config is optional.  Many of the config parameters are a a maximum value
    to use when selecting a random value from a range.  To always use the maximum
    value, set no_random to true.  The config is a dict containing some or all of:

    seed: [no default] seed the random number generator

    randomize: [default: true] enables randomization and use the max/min values

    max_thrash: [default: 1] the maximum number of MDSs that will be thrashed at
      any given time.

    max_thrash_delay: [default: 30] maximum number of seconds to delay before
      thrashing again.

    max_revive_delay: [default: 10] maximum number of seconds to delay before
      bringing back a thrashed MDS

    thrash_in_replay: [default: 0.0] likelihood that the MDS will be thrashed
      during replay.  Value should be between 0.0 and 1.0

    max_replay_thrash_delay: [default: 4] maximum number of seconds to delay while in
      the replay state before thrashing

    thrash_weights: allows specific MDSs to be thrashed more/less frequently.  This option
      overrides anything specified by max_thrash.  This option is a dict containing
      mds.x: weight pairs.  For example, [mds.a: 0.7, mds.b: 0.3, mds.c: 0.0].  Each weight
      is a value from 0.0 to 1.0.  Any MDSs not specified will be automatically
      given a weight of 0.0.  For a given MDS, by default the trasher delays for up
      to max_thrash_delay, trashes, waits for the MDS to recover, and iterates.  If a non-zero
      weight is specified for an MDS, for each iteration the thrasher chooses whether to thrash
      during that iteration based on a random value [0-1] not exceeding the weight of that MDS.

    Examples::


      The following example sets the likelihood that mds.a will be thrashed
      to 80%, mds.b to 20%, and other MDSs will not be thrashed.  It also sets the
      likelihood that an MDS will be thrashed in replay to 40%.
      Thrash weights do not have to sum to 1.

      tasks:
      - ceph:
      - mds_thrash:
          thrash_weights:
            - mds.a: 0.8
            - mds.b: 0.2
          thrash_in_replay: 0.4
      - ceph-fuse:
      - workunit:
          clients:
            all: [suites/fsx.sh]

      The following example disables randomization, and uses the max delay values:

      tasks:
      - ceph:
      - mds_thrash:
          max_thrash_delay: 10
          max_revive_delay: 1
          max_replay_thrash_delay: 4

    """

    def __init__(self, ctx, manager, mds_cluster, config, logger, failure_group, weight):
        super(MDSThrasher, self).__init__()

        self.ctx = ctx
        self.manager = manager
        assert self.manager.is_clean()
        self.mds_cluster = mds_cluster

        self.stopping = Event()
        self.logger = logger
        self.config = config

        self.randomize = bool(self.config.get('randomize', True))
        self.max_thrash_delay = float(self.config.get('thrash_delay', 30.0))
        self.thrash_in_replay = float(self.config.get('thrash_in_replay', False))
        assert self.thrash_in_replay >= 0.0 and self.thrash_in_replay <= 1.0, 'thrash_in_replay ({v}) must be between [0.0, 1.0]'.format(
            v=self.thrash_in_replay)

        self.max_replay_thrash_delay = float(self.config.get('max_replay_thrash_delay', 4.0))

        self.max_revive_delay = float(self.config.get('max_revive_delay', 10.0))

        self.failure_group = failure_group
        self.weight = weight

        # TODO support multiple filesystems: will require behavioural change to select
        # which filesystem to act on when doing rank-ish things
        self.fs = Filesystem(self.ctx)

    def _run(self):
        try:
            self.do_thrash()
        except:
            # Log exceptions here so we get the full backtrace (it's lost
            # by the time someone does a .get() on this greenlet)
            self.logger.exception("Exception in do_thrash:")
            raise

    def log(self, x):
        """Write data to logger assigned to this MDThrasher"""
        self.logger.info(x)

    def stop(self):
        self.stopping.set()

    def kill_mds(self, mds):
        if self.config.get('powercycle'):
            (remote,) = (self.ctx.cluster.only('mds.{m}'.format(m=mds)).
                         remotes.iterkeys())
            self.log('kill_mds on mds.{m} doing powercycle of {s}'.
                     format(m=mds, s=remote.name))
            self._assert_ipmi(remote)
            remote.console.power_off()
        else:
            self.ctx.daemons.get_daemon('mds', mds).stop()

    @staticmethod
    def _assert_ipmi(remote):
        assert remote.console.has_ipmi_credentials, (
            "powercycling requested but RemoteConsole is not "
            "initialized.  Check ipmi config.")

    def kill_mds_by_rank(self, rank):
        """
        kill_mds wrapper to kill based on rank passed.
        """
        status = self.mds_cluster.get_mds_info_by_rank(rank)
        self.kill_mds(status['name'])

    def revive_mds(self, mds, standby_for_rank=None):
        """
        Revive mds -- do an ipmpi powercycle (if indicated by the config)
        and then restart (using --hot-standby if specified.
        """
        if self.config.get('powercycle'):
            (remote,) = (self.ctx.cluster.only('mds.{m}'.format(m=mds)).
                         remotes.iterkeys())
            self.log('revive_mds on mds.{m} doing powercycle of {s}'.
                     format(m=mds, s=remote.name))
            self._assert_ipmi(remote)
            remote.console.power_on()
            self.manager.make_admin_daemon_dir(self.ctx, remote)
        args = []
        if standby_for_rank:
            args.extend(['--hot-standby', standby_for_rank])
        self.ctx.daemons.get_daemon('mds', mds).restart(*args)

    def revive_mds_by_rank(self, rank, standby_for_rank=None):
        """
        revive_mds wrapper to revive based on rank passed.
        """
        status = self.mds_cluster.get_mds_info_by_rank(rank)
        self.revive_mds(status['name'], standby_for_rank)

    def get_mds_status_all(self):
        return self.fs.get_mds_map()

    def do_thrash(self):
        """
        Perform the random thrashing action
        """

        self.log('starting mds_do_thrash for failure group: ' + ', '.join(
            ['mds.{_id}'.format(_id=_f) for _f in self.failure_group]))
        while not self.stopping.is_set():
            delay = self.max_thrash_delay
            if self.randomize:
                delay = random.randrange(0.0, self.max_thrash_delay)

            if delay > 0.0:
                self.log('waiting for {delay} secs before thrashing'.format(delay=delay))
                self.stopping.wait(delay)
                if self.stopping.is_set():
                    continue

            skip = random.randrange(0.0, 1.0)
            if self.weight < 1.0 and skip > self.weight:
                self.log('skipping thrash iteration with skip ({skip}) > weight ({weight})'.format(skip=skip,
                                                                                                   weight=self.weight))
                continue

            # find the active mds in the failure group
            statuses = [self.mds_cluster.get_mds_info(m) for m in self.failure_group]
            actives = filter(lambda s: s and s['state'] == 'up:active', statuses)
            assert len(actives) == 1, 'Can only have one active in a failure group'

            active_mds = actives[0]['name']
            active_rank = actives[0]['rank']

            self.log('kill mds.{id} (rank={r})'.format(id=active_mds, r=active_rank))
            self.kill_mds_by_rank(active_rank)

            # wait for mon to report killed mds as crashed
            last_laggy_since = None
            itercount = 0
            while True:
                failed = self.fs.get_mds_map()['failed']
                status = self.mds_cluster.get_mds_info(active_mds)
                if not status:
                    break
                if 'laggy_since' in status:
                    last_laggy_since = status['laggy_since']
                    break
                if any([(f == active_mds) for f in failed]):
                    break
                self.log(
                    'waiting till mds map indicates mds.{_id} is laggy/crashed, in failed state, or mds.{_id} is removed from mdsmap'.format(
                        _id=active_mds))
                itercount = itercount + 1
                if itercount > 10:
                    self.log('mds map: {status}'.format(status=self.mds_cluster.get_fs_map()))
                time.sleep(2)
            if last_laggy_since:
                self.log(
                    'mds.{_id} reported laggy/crashed since: {since}'.format(_id=active_mds, since=last_laggy_since))
            else:
                self.log('mds.{_id} down, removed from mdsmap'.format(_id=active_mds, since=last_laggy_since))

            # wait for a standby mds to takeover and become active
            takeover_mds = None
            takeover_rank = None
            itercount = 0
            while True:
                statuses = [self.mds_cluster.get_mds_info(m) for m in self.failure_group]
                actives = filter(lambda s: s and s['state'] == 'up:active', statuses)
                if len(actives) > 0:
                    assert len(actives) == 1, 'Can only have one active in failure group'
                    takeover_mds = actives[0]['name']
                    takeover_rank = actives[0]['rank']
                    break
                itercount = itercount + 1
                if itercount > 10:
                    self.log('mds map: {status}'.format(status=self.mds_cluster.get_fs_map()))

            self.log('New active mds is mds.{_id}'.format(_id=takeover_mds))

            # wait for a while before restarting old active to become new
            # standby
            delay = self.max_revive_delay
            if self.randomize:
                delay = random.randrange(0.0, self.max_revive_delay)

            self.log('waiting for {delay} secs before reviving mds.{id}'.format(
                delay=delay, id=active_mds))
            time.sleep(delay)

            self.log('reviving mds.{id}'.format(id=active_mds))
            self.revive_mds(active_mds, standby_for_rank=takeover_rank)

            status = {}
            while True:
                status = self.mds_cluster.get_mds_info(active_mds)
                if status and (status['state'] == 'up:standby' or status['state'] == 'up:standby-replay'):
                    break
                self.log(
                    'waiting till mds map indicates mds.{_id} is in standby or standby-replay'.format(_id=active_mds))
                time.sleep(2)
            self.log('mds.{_id} reported in {state} state'.format(_id=active_mds, state=status['state']))

            # don't do replay thrashing right now
            continue
            # this might race with replay -> active transition...
            if status['state'] == 'up:replay' and random.randrange(0.0, 1.0) < self.thrash_in_replay:

                delay = self.max_replay_thrash_delay
                if self.randomize:
                    delay = random.randrange(0.0, self.max_replay_thrash_delay)
                time.sleep(delay)
                self.log('kill replaying mds.{id}'.format(id=self.to_kill))
                self.kill_mds(self.to_kill)

                delay = self.max_revive_delay
                if self.randomize:
                    delay = random.randrange(0.0, self.max_revive_delay)

                self.log('waiting for {delay} secs before reviving mds.{id}'.format(
                    delay=delay, id=self.to_kill))
                time.sleep(delay)

                self.log('revive mds.{id}'.format(id=self.to_kill))
                self.revive_mds(self.to_kill)


@contextlib.contextmanager
def task(ctx, config):
    """
    Stress test the mds by thrashing while another task/workunit
    is running.

    Please refer to MDSThrasher class for further information on the
    available options.
    """

    mds_cluster = MDSCluster(ctx)

    if config is None:
        config = {}
    assert isinstance(config, dict), \
        'mds_thrash task only accepts a dict for configuration'
    mdslist = list(teuthology.all_roles_of_type(ctx.cluster, 'mds'))
    assert len(mdslist) > 1, \
        'mds_thrash task requires at least 2 metadata servers'

    # choose random seed
    if 'seed' in config:
        seed = int(config['seed'])
    else:
        seed = int(time.time())
    log.info('mds thrasher using random seed: {seed}'.format(seed=seed))
    random.seed(seed)

    max_thrashers = config.get('max_thrash', 1)
    thrashers = {}

    (first,) = ctx.cluster.only('mds.{_id}'.format(_id=mdslist[0])).remotes.iterkeys()
    manager = ceph_manager.CephManager(
        first, ctx=ctx, logger=log.getChild('ceph_manager'),
    )

    # make sure everyone is in active, standby, or standby-replay
    log.info('Wait for all MDSs to reach steady state...')
    statuses = None
    statuses_by_rank = None
    while True:
        statuses = {m: mds_cluster.get_mds_info(m) for m in mdslist}
        statuses_by_rank = {}
        for _, s in statuses.iteritems():
            if isinstance(s, dict):
                statuses_by_rank[s['rank']] = s

        ready = filter(lambda (_, s): s is not None and (s['state'] == 'up:active'
                                                         or s['state'] == 'up:standby'
                                                         or s['state'] == 'up:standby-replay'),
                       statuses.items())
        if len(ready) == len(statuses):
            break
        time.sleep(2)
    log.info('Ready to start thrashing')

    # setup failure groups
    failure_groups = {}
    actives = {s['name']: s for (_, s) in statuses.iteritems() if s['state'] == 'up:active'}
    log.info('Actives is: {d}'.format(d=actives))
    log.info('Statuses is: {d}'.format(d=statuses_by_rank))
    for active in actives:
        for (r, s) in statuses.iteritems():
            if s['standby_for_name'] == active:
                if not active in failure_groups:
                    failure_groups[active] = []
                log.info('Assigning mds rank {r} to failure group {g}'.format(r=r, g=active))
                failure_groups[active].append(r)

    manager.wait_for_clean()
    for (active, standbys) in failure_groups.iteritems():
        weight = 1.0
        if 'thrash_weights' in config:
            weight = int(config['thrash_weights'].get('mds.{_id}'.format(_id=active), '0.0'))

        failure_group = [active]
        failure_group.extend(standbys)

        thrasher = MDSThrasher(
            ctx, manager, mds_cluster, config,
            logger=log.getChild('mds_thrasher.failure_group.[{a}, {sbs}]'.format(
                a=active,
                sbs=', '.join(standbys)
            )
            ),
            failure_group=failure_group,
            weight=weight)
        thrasher.start()
        thrashers[active] = thrasher

        # if thrash_weights isn't specified and we've reached max_thrash,
        # we're done
        if 'thrash_weights' not in config and len(thrashers) == max_thrashers:
            break

    try:
        log.debug('Yielding')
        yield
    finally:
        log.info('joining mds_thrashers')
        for t in thrashers:
            log.info('join thrasher for failure group [{fg}]'.format(fg=', '.join(failure_group)))
            thrashers[t].stop()
            thrashers[t].get()  # Raise any exception from _run()
            thrashers[t].join()
        log.info('done joining')
