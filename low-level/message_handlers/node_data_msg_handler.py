"""
 ****************************************************************************
 Filename:          node_data_msg_handler.py
 Description:       Message Handler for requesting node data
 Creation Date:     06/18/2015
 Author:            Jake Abernathy

 Do NOT modify or remove this copyright and confidentiality notice!
 Copyright (c) 2001 - $Date: 2015/01/14 $ Seagate Technology, LLC.
 The code contained herein is CONFIDENTIAL to Seagate Technology, LLC.
 Portions are also trade secret. Any use, duplication, derivation, distribution
 or disclosure of this code, for any reason, not expressly authorized is
 prohibited. All other rights are expressly reserved by Seagate Technology, LLC.
 ****************************************************************************
"""

import json
import time

from actuators.ILogin import ILogin
from sensors.INode_data import INodeData

from framework.base.module_thread import ScheduledModuleThread
from framework.base.internal_msgQ import InternalMsgQ
from framework.utils.service_logging import logger

from json_msgs.messages.sensors.host_update import HostUpdateMsg
from json_msgs.messages.sensors.local_mount_data import LocalMountDataMsg
from json_msgs.messages.sensors.cpu_data import CPUdataMsg
from json_msgs.messages.sensors.if_data import IFdataMsg
from json_msgs.messages.sensors.raid_data import RAIDdataMsg

from rabbitmq.rabbitmq_egress_processor import RabbitMQegressProcessor 

from zope.component import queryUtility


class NodeDataMsgHandler(ScheduledModuleThread, InternalMsgQ):
    """Message Handler for generic node requests and generating
        host update messages on a regular interval"""

    MODULE_NAME = "NodeDataMsgHandler"
    PRIORITY    = 2

    # Section and keys in configuration file
    NODEDATAMSGHANDLER = MODULE_NAME.upper()
    TRANSMIT_INTERVAL = 'transmit_interval'
    UNITS = 'units'

    @staticmethod
    def name():
        """ @return: name of the module."""
        return NodeDataMsgHandler.MODULE_NAME

    def __init__(self):
        super(NodeDataMsgHandler, self).__init__(self.MODULE_NAME,
                                                  self.PRIORITY)

    def initialize(self, conf_reader, msgQlist):
        """initialize configuration reader and internal msg queues"""
        # Initialize ScheduledMonitorThread
        super(NodeDataMsgHandler, self).initialize(conf_reader)

        # Initialize internal message queues for this module
        super(NodeDataMsgHandler, self).initialize_msgQ(msgQlist)

        self._transmit_interval = int(self._conf_reader._get_value_with_default(
                                                self.NODEDATAMSGHANDLER, 
                                                self.TRANSMIT_INTERVAL,
                                                60))
        self._units = self._conf_reader._get_value_with_default(
                                                self.NODEDATAMSGHANDLER, 
                                                self.UNITS,
                                                "MB")
        self._node_sensor    = None
        self._login_actuator = None
        self._RAID_status    = "N/A"

    def run(self):
        """Run the module periodically on its own thread."""
        self._log_debug("Start accepting requests")

        # self._set_debug(True)
        # self._set_debug_persist(True)

        try:
            # Query the Zope GlobalSiteManager for an object implementing INodeData
            if self._node_sensor is None:
                self._node_sensor = queryUtility(INodeData)()
                self._log_debug("_node_sensor name: %s" % self._node_sensor.name())           

            # Delay for the desired interval if it's greater than zero            
            if self._transmit_interval > 0:
                timer = self._transmit_interval
                while timer > 0:
                    # See if the message queue contains an entry and process
                    jsonMsg = self._read_my_msgQ_noWait()
                    if jsonMsg is not None:
                        self._process_msg(jsonMsg)

                    time.sleep(1)
                    timer -= 1

                # Generate the JSON messages with data from the node and transmit on regular interval
                self._generate_host_update()
                self._generate_local_mount_data()
                self._generate_cpu_data()
                self._generate_if_data()

            # If the timer is zero then block for incoming requests notifying to transmit data
            else:
                # Block on message queue until it contains an entry
                jsonMsg = self._read_my_msgQ()
                if jsonMsg is not None:
                    self._process_msg(jsonMsg)

                # Keep processing until the message queue is empty
                while not self._is_my_msgQ_empty():
                    jsonMsg = self._read_my_msgQ()
                    if jsonMsg is not None:
                        self._process_msg(jsonMsg)

        except Exception as ae:
            # Log it and restart the whole process when a failure occurs
            logger.exception("NodeDataMsgHandler restarting: %s" % ae)

        self._scheduler.enter(1, self._priority, self.run, ())
        self._log_debug("Finished processing successfully")

    def _process_msg(self, jsonMsg):
        """Parses the incoming message and generate the desired data message"""
        self._log_debug("_process_msg, jsonMsg: %s" % jsonMsg)

        if isinstance(jsonMsg, dict) == False:
            jsonMsg = json.loads(jsonMsg)

        # Parse out the uuid so that it can be sent back in response message
        self._uuid = None
        if jsonMsg.get("sspl_ll_msg_header") is not None and \
           jsonMsg.get("sspl_ll_msg_header").get("uuid") is not None:
            self._uuid = jsonMsg.get("sspl_ll_msg_header").get("uuid")
            self._log_debug("_processMsg, uuid: %s" % self._uuid)

        if jsonMsg.get("sensor_request_type").get("node_data").get("sensor_type") is not None:
            sensor_type = jsonMsg.get("sensor_request_type").get("node_data").get("sensor_type")
            self._log_debug("_processMsg, sensor_type: %s" % sensor_type)

            if sensor_type == "host_update":
                self._generate_host_update()

            elif sensor_type == "local_mount_data":
                self._generate_local_mount_data()

            elif sensor_type == "cpu_data":
                self._generate_cpu_data()

            elif sensor_type == "if_data":
                self._generate_if_data()

            elif sensor_type == "host_update_all":
                self._generate_host_update()
                self._generate_local_mount_data()
                self._generate_cpu_data()
                self._generate_if_data()

            elif sensor_type == "raid_data":
                self._generate_RAID_status(jsonMsg)


        # ... handle other node sensor message types

    def _generate_host_update(self):
        """Create & transmit a host update message as defined
            by the sensor response json schema"""

        # Notify the node sensor to update its data required for the host_update message
        successful = self._node_sensor.read_data("host_update", self._get_debug(), self._units)
        if not successful:
            logger.error("NodeDataMsgHandler, _generate_host_update was NOT successful.")

        # Query the Zope GlobalSiteManager for an object implementing ILogin
        if self._login_actuator is None:
            self._login_actuator = queryUtility(ILogin)()
            self._log_debug("_generate_host_update, login_actuator name: %s" % self._login_actuator.name())

        # Notify the login actuator to update its data of logged in users
        login_request={"login_request": "get_all_users"}
        logged_in_users = self._login_actuator.perform_request(login_request)

        # Create the host update message and hand it over to the egress processor to transmit
        hostUpdateMsg = HostUpdateMsg(self._node_sensor.host_id,
                                self._node_sensor.local_time,
                                self._node_sensor.boot_time,
                                self._node_sensor.up_time,
                                self._node_sensor.uname, self._units,
                                self._node_sensor.free_mem,
                                self._node_sensor.total_mem, logged_in_users,
                                self._node_sensor.process_count,
                                self._node_sensor.running_process_count
                                )

        # Add in uuid if it was present in the json request
        if self._uuid is not None:
            hostUpdateMsg.set_uuid(self._uuid)
        jsonMsg = hostUpdateMsg.getJson()

        # Transmit it out over rabbitMQ channel
        self._write_internal_msgQ(RabbitMQegressProcessor.name(), jsonMsg)

    def _generate_local_mount_data(self):
        """Create & transmit a local_mount_data message as defined
            by the sensor response json schema"""

        # Notify the node sensor to update its data required for the local_mount_data message
        successful = self._node_sensor.read_data("local_mount_data", self._get_debug(), self._units)
        if not successful:
            logger.error("NodeDataMsgHandler, _generate_local_mount_data was NOT successful.")

        # Create the local mount data message and hand it over to the egress processor to transmit
        localMountDataMsg = LocalMountDataMsg(self._node_sensor.host_id,
                                self._node_sensor.local_time,
                                self._node_sensor.free_space,
                                self._node_sensor.free_inodes,
                                self._node_sensor.free_swap,
                                self._node_sensor.total_space,
                                self._node_sensor.total_swap,
                                self._units)

        # Add in uuid if it was present in the json request
        if self._uuid is not None:
            localMountDataMsg.set_uuid(self._uuid)
        jsonMsg = localMountDataMsg.getJson()

        # Transmit it out over rabbitMQ channel
        self._write_internal_msgQ(RabbitMQegressProcessor.name(), jsonMsg)

    def _generate_cpu_data(self):
        """Create & transmit a cpu_data message as defined
            by the sensor response json schema"""

        # Notify the node sensor to update its data required for the cpu_data message
        successful = self._node_sensor.read_data("cpu_data", self._get_debug())
        if not successful:
            logger.error("NodeDataMsgHandler, _generate_cpu_data was NOT successful.")

        # Create the local mount data message and hand it over to the egress processor to transmit
        cpuDataMsg = CPUdataMsg(self._node_sensor.host_id,
                             self._node_sensor.local_time,
                             self._node_sensor.csps,
                             self._node_sensor.idle_time,
                             self._node_sensor.interrupt_time,
                             self._node_sensor.iowait_time,
                             self._node_sensor.nice_time,
                             self._node_sensor.softirq_time,
                             self._node_sensor.steal_time,
                             self._node_sensor.system_time,
                             self._node_sensor.user_time,
                             self._node_sensor.cpu_core_data)

        # Add in uuid if it was present in the json request
        if self._uuid is not None:
            cpuDataMsg.set_uuid(self._uuid)
        jsonMsg = cpuDataMsg.getJson()

        # Transmit it out over rabbitMQ channel
        self._write_internal_msgQ(RabbitMQegressProcessor.name(), jsonMsg)

    def _generate_if_data(self):
        """Create & transmit a network interface data message as defined
            by the sensor response json schema"""

        # Notify the node sensor to update its data required for the if_data message
        successful = self._node_sensor.read_data("if_data", self._get_debug())
        if not successful:
            logger.error("NodeDataMsgHandler, _generate_if_data was NOT successful.")

        ifDataMsg = IFdataMsg(self._node_sensor.host_id,
                            self._node_sensor.local_time,
                            self._node_sensor.if_data)

        # Add in uuid if it was present in the json request
        if self._uuid is not None:
            ifDataMsg.set_uuid(self._uuid)
        jsonMsg = ifDataMsg.getJson()

        # Transmit it out over rabbitMQ channel
        self._write_internal_msgQ(RabbitMQegressProcessor.name(), jsonMsg)

    def _generate_RAID_status(self, jsonMsg):
        """Create & transmit a RAID status data message as defined
            by the sensor response json schema"""
        # Get the host_id
        if self._node_sensor.host_id == None:
            successful = self._node_sensor.read_data("None", self._get_debug(), self._units)
            if not successful:
                logger.error("NodeDataMsgHandler, updating host information was NOT successful.")

        # See if status is in the msg; ie it's an internal msg from the RAID sensor
        if jsonMsg.get("sensor_request_type").get("node_data").get("status") is not None:
            self._RAID_status = jsonMsg.get("sensor_request_type").get("node_data").get("status")

        self._log_debug("_generate_RAID_status, host_id: %s, RAID_status: %s" % 
                            (self._node_sensor.host_id, self._RAID_status))

        raidDataMsg = RAIDdataMsg(self._node_sensor.host_id,
                              self._RAID_status)

        # Add in uuid if it was present in the json request
        if self._uuid is not None:
            raidDataMsg.set_uuid(self._uuid)
        jsonMsg = raidDataMsg.getJson()

        # Transmit it out over rabbitMQ channel
        self._write_internal_msgQ(RabbitMQegressProcessor.name(), jsonMsg)

    def shutdown(self):
        """Clean up scheduler queue and gracefully shutdown thread"""
        super(NodeDataMsgHandler, self).shutdown()