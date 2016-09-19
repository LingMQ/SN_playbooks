#!/usr/bin/env python
"""Capture playbook result and send to Jenkins or the message bus.

Currently the script is deliverying message to Jenkins since the 
message bus UMB is not on production until some time Oct 2016. 
To enable the functionality of deliverying message to UMB,
one need to change the global variable USE_UMB to True.

Also grabs information like the ServiceNow ticket URL from playbook
variables. Can be included with a role and enabled/disabled via
playbook variable `report_to_messagebus`, which can be specified as an
extra variable, in a role, or elsewhere.

"""

# NOTES: if sending message to UMB, need to install the proton lib first 

# NOTES: Variable file contains the necessary information for triggering the jenkins job in a local host.
# When swithing the environment, the user or the jenkins project, the variable file will need to be updated.


from __future__ import absolute_import, division, print_function, unicode_literals

import functools
import json
import os
import shutil
import sys
import tempfile
import time

#import jenkins
import requests

try:
    import cStringIO as StringIO
except:
    import StringIO
    
from ansible.utils.display import Display
from ansible.plugins.callback import CallbackBase


USE_UMB = False


class CaptureDisplay(Display):
    """Capture plugin output to stdout and stderr."""
    def __init__(self, *args, **kwargs):
        super(CaptureDisplay, self).__init__(*args, **kwargs)
        self._output = StringIO.StringIO()

    def display(self, *args, **kwargs):
        old_stdout = sys.stdout
        old_stderr = sys.stderr
        try:
            sys.stdout = self._output
            sys.stderr = self._output
            super(CaptureDisplay, self).display(*args, **kwargs)
        finally:
            sys.stdout = old_stdout
            sys.stderr = old_stderr


try:
    import proton
    from proton import Message, SSLDomain
    from proton.handlers import MessagingHandler
    from proton.reactor import Container

    class Sender(MessagingHandler):
        def __init__(self, server, topic, certificate, key, message):
            super(Sender, self).__init__()
            self.server = server
            self.topic = topic
            self.certificate = certificate
            self.key = key
            self.message = message

        def on_start(self, event):
            # Write the UMB cert and key out to disk but immediately delete
            # them once the connection has been established. There may be a
            # better way to do this if we can be assured of a secure directory.
            temp_dir = tempfile.mkdtemp()
            mktemp = functools.partial(tempfile.NamedTemporaryFile,
                                       delete=False,
                                       dir=temp_dir)
            
            try:
                temp_cert = mktemp()
                temp_key = mktemp()
                temp_cert.write(self.certificate)
                temp_key.write(self.key)
                temp_cert.close()
                temp_key.close()
                
                domain = SSLDomain(SSLDomain.MODE_CLIENT)
                domain.set_credentials(temp_cert.name, temp_key.name, b'')
                conn = event.container.connect(self.server, ssl_domain=domain)
            finally:
                shutil.rmtree(temp_dir)
                    
            event.container.create_sender(conn, "topic://" + self.topic)
        

        def on_sendable(self, event):
            message = Message(body=json.dumps(self.message))
            # We have to manually set this - Proton won't do it for us
            message.creation_time = time.time()
            print(message)
            event.sender.send(message)
            event.sender.close()
                                              

        def on_settled(self, event):
            event.connection.close()

                
    PROTON_AVAILABLE = True
except ImportError:
    PROTON_AVAILABLE = False


class CallbackModule(CallbackBase):
    CALLBACK_NAME = 'report_status'
    CALLBACK_TYPE = 'selfservice'

    def __init__(self, *args, **kwargs):
        super(CallbackModule, self).__init__(*args, **kwargs)#, CaptureDisplay(verbosity=4))
        self.ticket = None
        self.enabled = False
        self.status = 'success'
        self.output = CaptureDisplay(verbosity = 4)
        self.output.__init__

    def v2_playbook_on_play_start(self, play):
        super(CallbackModule, self).v2_playbook_on_play_start(play)

        if not PROTON_AVAILABLE:
            print("qpid-proton is not available, not reporting to message bus...")
            self.messagebus = None
            return

        manager = play.get_variable_manager()
        variables = manager.get_vars(play.get_loader(), play=play)

        self.ticket = variables.get('servicenow_url')
        self.messagebus = variables.get('message_bus')
        self.messagebus_topic = variables.get('message_bus_topic')
        self.messagebus_crt = variables.get('message_bus_cert')
        self.messagebus_key = variables.get('message_bus_key')

        self.jenkins_url_addr = variables.get('jenkins_url')
        print("print the jenkins_url_addr")
        print(self.jenkins_url_addr)
        self.jenkins_usrname = variables.get('jenkins_usr')
        self.jenkins_jobName = variables.get('jenkins_job_name')
        print("print the job name")
        print(self.jenkins_jobName)
        self.jenkins_api_token = variables.get('jenkins_usr_api_token')

        
    def v2_runner_on_failed(self, result, ignore_errors=False):
        super(CallbackModule, self).v2_runner_on_failed(result, ignore_errors)
        if not ignore_errors:
            self.status = 'failure'

    def v2_runner_on_unreachable(self, result):
        super(CallbackModule, self).v2_runner_on_unreachable(result)
        self.status = 'failure'

    def v2_playbook_on_stats(self, stats):
        super(CallbackModule, self).v2_playbook_on_stats(stats)
        if not self.messagebus:
            return

        outputInfo = self.output._output.getvalue()
        status_message = {
            'status': self.status,
            'job_id': os.environ.get('JOB_ID', None),
            'servicenow_url': self.ticket,
            'user_data': None, #non for ansible for now
            'output': outputInfo,
        }
        print(status_message)

        if USE_UMB:
            Container(Sender(
                self.messagebus, self.messagebus_topic,
                self.messagebus_crt, self.messagebus_key,
                status_message)).run()
        else:
            '''
            print("debugging line: before trigger the jenkins job")           
            j = jenkins.Jenkins(self.jenkins_url_addr, self.jenkins_usrname, self.jenkins_api_token)
            j.build_job(self.jenkins_jobName, parameters=status_message)
            print("debugging line: after trigger the jenkins job")
            '''
            
            ## use request instead
            url = self.jenkins_url_addr
            resp = requests.post(url, 
                                 auth=(self.jenkins_usrname, self.jenkins_api_token),
                                 json=status_message,
                                 headers={"Content-Type": "application/json",},
                                 verify=False,)
