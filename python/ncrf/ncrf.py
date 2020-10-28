import datetime
import time
from ncs.dp import Action
import ncs
import _ncs

TIMEOUT = 30


class ncrf_common(ncs.dp.Action):

    def set_trans_timeout(self, log_prefix, uinfo, log):
        log.debug(log_prefix, " - SETTING TIMEOUT TO ", TIMEOUT)
        _ncs.dp.action_set_timeout(uinfo, TIMEOUT)

    def log_terminal(self, maapi, uinfo, msg, is_title):
        if is_title:
            msg = '\n{}\n{}\n{}\n'.format(len(msg)*'*', msg, len(msg)*'*')
        maapi.cli_write(uinfo.usid, '{}\n'.format(msg))


class Generic_ValidateService(ncrf_common):
    @Action.action
    def cb_action(self, uinfo, name, kp, input, output):

        self.log.debug("Starting validate-service action")

        with ncs.maapi.Maapi() as m:
            with ncs.maapi.Session(m, 'admin', 'system'):
                with m.start_read_trans() as t:

                    self.root = ncs.maagic.get_root(t)
                    self.uinfo = uinfo

                    self.log.debug('kp: {}'.format(kp))
                    # Get instance using the action keypath
                    self.instance = ncs.maagic.get_node(t, str(kp))
                    self.log.debug('instance name: {}'.format(
                        self.instance.ncrf_service_id))

                    (output.result, output.message, output.dry_run) = (
                        self.validate_standards())

    def validate_standards(self):

        self.set_trans_timeout('VALIDATE_STANDARD', self.uinfo, self.log)

        id = self.instance.ncrf_service_id

        service_list = self.get_service_list(self.root)

        if id in service_list:
            msg = "Service instance already exists."
            self.log.debug(msg)
            return "Failure", msg, ''

        return self.validate_standard(
            self.instance._path,
            service_list._path,
            id)

    def validate_standard(self, src_path, dst_list_path, id):
        dst_path = None
        dry_run = None
        # temporarily create service instance
        with ncs.maapi.Maapi() as m:
            with ncs.maapi.Session(m, 'admin', 'system'):
                with m.start_write_trans() as t:
                    dst_list = ncs.maagic.get_node(t, dst_list_path)
                    dst = dst_list.create(id)
                    dst_path = dst._path
                    self.log.debug('src: ', src_path)
                    self.log.debug('dst: ', dst_path)

                    # copy instance can be customized
                    self.copy_instance(m, t, src_path, dst_path)

                    t.apply(flags=ncs.maapi.COMMIT_NCS_NO_NETWORKING)

                    self.log.debug('Instance created.')

        # dry-run reconcile + discard to check if extraneous configs exist
        # remove temp service instance
        with ncs.maapi.Maapi() as m:
            with ncs.maapi.Session(m, 'admin', 'system'):
                with m.start_write_trans() as t:
                    instance = ncs.maagic.get_node(t, dst_path)
                    inputs = instance.re_deploy.get_input()
                    inputs.reconcile.create()
                    inputs.reconcile.discard_non_service_config.create()
                    inputs.dry_run.create()
                    inputs.dry_run.outformat = 'native'

                    self.log.debug('Instance re-deploy dry-run...')
                    outputs = instance.re_deploy(inputs)
                    dry_run = outputs.native
                    self.log.debug('Deleting instance...')
                    dst_list = ncs.maagic.get_node(t, dst_list_path)
                    del dst_list[id]
                    t.apply(flags=ncs.maapi.COMMIT_NCS_NO_NETWORKING)
                    self.log.debug('Instance deleted.')

        self.log.debug('dry-run collected:')
        r = ''
        for d in dry_run.device:
            r = '{}\n{}\n{}'.format(r, d.name, d.data)
            self.log.debug('{}\n{}\n'.format(d.name, d.data))
            self.log.debug('***********************************************')

        return "Success", "instance validated", str(r)

    def get_service_list(self, root):
        raise NotImplementedError

    def copy_instance(self, m, t, src_path, dst_path):
        self.log.debug('default copy isntance')
        m.copy_tree(t.th, src_path, dst_path)


class Generic_ReconcileService(ncs.dp.Action):
    @Action.action
    def cb_action(self, uinfo, name, kp, input, output):

        self.log.debug("Starting reconcile-service action")
        try:
            self.create_instance(kp)

        except Exception as e:
            msg = "instance creation failed: {}".format(e)
            self.log.error(msg)
            output.result = "Failure"
            output.message = msg
            return

        try:
            output.result, output.message = self.reconcile_instance(kp)

        except Exception as e:
            msg = "instance reconciliation failed: {}".format(e)
            self.log.error(msg)
            # delete service instance to rollback the changes
            self.delete_instance(kp)
            output.result = "Failure"
            output.message = msg
            return

    def create_instance(self, kp):

        with ncs.maapi.Maapi() as m:
            with ncs.maapi.Session(m, 'admin', 'system'):
                with m.start_write_trans() as t:

                    root = ncs.maagic.get_root(t)
                    self.log.debug('kp: {}'.format(kp))
                    # Get instance using the action keypath
                    src = ncs.maagic.get_node(t, str(kp))

                    # check if reconcilation-enabled is True
                    if self.is_service_confirmed(src) is False:
                        msg = 'Reconciliation not enabled. Exiting...'
                        self.log.error(msg)
                        raise Exception(msg)

                    key = self.get_service_key(src)
                    self.log.debug('Candidate name: {}'.format(key))

                    if not key:
                        msg = ("Service name is not defined. Exiting...")
                        self.log.error(msg)
                        raise Exception(msg)

                    service_list = self.get_service_list(root)

                    if key in service_list:
                        msg = ("Service intsance already exists! Exiting...")
                        self.log.error(msg)
                        raise Exception(msg)

                    # create service instance
                    self.log.debug(
                        'Creating service instance {}...'.format(key))
                    dst = service_list.create(key)

                    # copy instance can be customized
                    self.copy_instance(m, t, src._path, dst._path)

                    # commit transaction
                    t.apply(flags=ncs.maapi.COMMIT_NCS_NO_NETWORKING)
                    self.log.debug('Instance created.')

    def reconcile_instance(self, kp):

        with ncs.maapi.Maapi() as m:
            with ncs.maapi.Session(m, 'admin', 'system'):
                with m.start_read_trans() as t:
                    # Get service insance
                    root = ncs.maagic.get_root(t)
                    src = ncs.maagic.get_node(t, str(kp))

                    key = self.get_service_key(src)
                    service_list = self.get_service_list(root)

                    instance = service_list[key]

                    # redeploy reconcile
                    action_input = instance.re_deploy.get_input()
                    action_input.reconcile.create()
                    action_input.no_networking.create()

                    self.log.debug('Redeploying...')
                    instance.re_deploy(action_input)
                    self.log.debug('Instance reconciled.')

                    return "Success", "instance reconciled"

    def delete_instance(self, kp):

        with ncs.maapi.Maapi() as m:
            with ncs.maapi.Session(m, 'admin', 'system'):
                with m.start_write_trans() as t:
                    root = ncs.maagic.get_root(t)

                    service_list = self.get_service_list(root)

                    # Get instance using the action keypath
                    src = ncs.maagic.get_node(t, str(kp))
                    key = self.get_service_key(src)
                    self.log.debug('Aborting. deleting instance: ', key)
                    del service_list[key]

                    t.apply(flags=ncs.maapi.COMMIT_NCS_NO_NETWORKING)

    def is_service_confirmed(self, src):
        return src.confirmed

    def get_service_key(self, src):
        return src.ncrf_service_id

    def get_service_list(self, root):
        raise NotImplementedError


class Generic_DiscoverServices(Action):
    @Action.action
    def cb_action(self, uinfo, name, kp, input, output):

        self.log.debug("Starting discover-services action")

        start_time = time.time()

        list_name = "Services-" + datetime.datetime.now().strftime(
            "%Y-%m-%dT%H:%M:%S")

        with ncs.maapi.Maapi() as m:
            with ncs.maapi.Session(m, 'admin', 'system'):
                with m.start_write_trans() as t:

                    root = ncs.maagic.get_root(t)

                    output.list_name = list_name

                    self.log.debug('Collecting services under name: {}'.format(
                        list_name))

                    ncrf_path = self.get_ncrf_path(root)

                    db_service_list = ncrf_path.discovered_service_list.create(
                        list_name)

                    n_services = self.custom_discover_services(
                        root, input.device_name, db_service_list)

                    t.apply()

                    end_time = time.time()

                    output.message = (
                        "Found {} services). Elapsed time is {} sec".format(
                            n_services, end_time - start_time))

    def get_ncrf_path(self, root):
        raise NotImplementedError

    def custom_discover_services(self, root, db_service_list):
        raise NotImplementedError


class Generic_PopulateService(Action):

    @Action.action
    def cb_action(self, uinfo, name, kp, input, output):

        self.log.debug("Starting populate-service action")

        with ncs.maapi.Maapi() as m:
            with ncs.maapi.Session(m, 'admin', 'system'):
                with m.start_write_trans() as t:

                    root = ncs.maagic.get_root(t)

                    self.log.debug('kp: {}'.format(kp))

                    # Get instance using the action keypath
                    instance = ncs.maagic.get_node(t, str(kp))

                    try:
                        self.log.debug('instance name: {}'.format(
                            instance.ncrf_service_id))

                        # check if reconcilation-enabled is True
                        if instance.confirmed is False:
                            msg = 'Reconciliation not enabled, exiting'
                            self.log.debug(msg)
                            output.message = msg
                            output.result = 'Failure'
                            t.apply()
                            return

                    except Exception as e:
                        self.log.debug('Failed fetching instance ncrf data: ',
                                       e)

                    (output.result, output.message) = self.populate_service(
                        root, instance)

                    t.apply()

    def populate_service(self, root, instance):
        raise NotImplementedError

    def set_flag(self, root, instance, flag):
        instance.flags.create(flag)
