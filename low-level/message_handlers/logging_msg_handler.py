"""
 ****************************************************************************
 Filename:          logging_msg_handler.py
 Description:       Message Handler for logging Messages
 Creation Date:     02/25/2015
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

from framework.base.module_thread import ScheduledModuleThread
from framework.base.internal_msgQ import InternalMsgQ
from framework.utils.service_logging import logger

from loggers.impl.iem_logger import IEMlogger

# Modules that receive messages from this module
from framework.rabbitmq.rabbitmq_egress_processor import RabbitMQegressProcessor

from json_msgs.messages.actuators.ack_response import AckResponseMsg

class LoggingMsgHandler(ScheduledModuleThread, InternalMsgQ):
    """Message Handler for logging Messages"""

    MODULE_NAME = "LoggingMsgHandler"
    PRIORITY    = 2

    # Section and keys in configuration file
    LOGGINGMSGHANDLER = MODULE_NAME.upper()


    @staticmethod
    def name():
        """ @return: name of the module."""
        return LoggingMsgHandler.MODULE_NAME

    def __init__(self):
        super(LoggingMsgHandler, self).__init__(self.MODULE_NAME,
                                                  self.PRIORITY)

    def initialize(self, conf_reader, msgQlist):
        """initialize configuration reader and internal msg queues"""
        # Initialize ScheduledMonitorThread
        super(LoggingMsgHandler, self).initialize(conf_reader)

        # Initialize internal message queues for this module
        super(LoggingMsgHandler, self).initialize_msgQ(msgQlist)

        self._conf_reader = conf_reader
        self._iem_logger  = None

    def run(self):
        """Run the module periodically on its own thread."""

        self._log_debug("Start accepting requests")

        try:
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
            logger.exception("LoggingMsgHandler restarting: %s" % ae)

        self._scheduler.enter(1, self._priority, self.run, ())
        self._log_debug("Finished processing successfully")

    def _process_msg(self, jsonMsg):
        """Parses the incoming message and hands off to the appropriate logger"""    
        self._log_debug("_process_msg, jsonMsg: %s" % jsonMsg)  

        if isinstance(jsonMsg, dict) == False:
            jsonMsg = json.loads(jsonMsg)

        uuid = None
        if jsonMsg.get("sspl_ll_msg_header") is not None and \
           jsonMsg.get("sspl_ll_msg_header").get("uuid") is not None:
            uuid = jsonMsg.get("sspl_ll_msg_header").get("uuid")
            self._log_debug("_process_msg, uuid: %s" % uuid)

        log_type = jsonMsg.get("actuator_request_type").get("logging").get("log_type")

        result = "N/A"
        if log_type == "IEM":
            self._log_debug("_processMsg, msg_type: IEM")
            if self._iem_logger == None:
                self._iem_logger = IEMlogger(self._conf_reader)
            result = self._iem_logger.log_msg(jsonMsg)

        if log_type == "HDS":
            # Retrieve the serial number of the drive
            self._log_debug("_processMsg, msg_type: HDS")
            log_msg = jsonMsg.get("actuator_request_type").get("logging").get("log_msg")

            # Parse out the json data section in the IEM and replace single quotes with double
            json_data = json.loads(str('{' + log_msg.split('{')[1]).replace("'", '"'))

            serial_number = json_data.get("serial_number")
            status = json_data.get("status")
            reason = json_data.get("reason")
            self._log_debug("_processMsg, serial_number: %s, status: %s, reason: %s"
                            % (serial_number, status, reason))

            # Send a message to the disk manager handler to create and transmit json msg
            internal_json_msg = json.dumps(
                 {"sensor_response_type" : "disk_status_HDS",
                  "object_path" : "HDS",
                  "status" : status,
                  "reason" : reason,
                  "serial_number" : serial_number
                 })

            # Send the event to disk message handler to generate json message
            self._write_internal_msgQ("DiskMsgHandler", internal_json_msg)

            # Hand off to the IEM logger
            if self._iem_logger == None:
                self._iem_logger = IEMlogger(self._conf_reader)
            result = self._iem_logger.log_msg(jsonMsg)

        # ... handle other logging types

        # Send ack about logging msg
        json_msg = AckResponseMsg(log_type, result, uuid).getJson()
        self._write_internal_msgQ(RabbitMQegressProcessor.name(), json_msg)

    def shutdown(self):
        """Clean up scheduler queue and gracefully shutdown thread"""
        super(LoggingMsgHandler, self).shutdown()