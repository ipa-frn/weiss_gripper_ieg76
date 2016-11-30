#!/usr/bin/env python
import roslib
import struct
import time
import serial
import rospy
import threading
import binascii
from serial import SerialException
from std_srvs.srv import Empty, EmptyResponse, Trigger, TriggerResponse

serial_port_lock = threading.Lock()
status_flags_cond_var = threading.Condition()
jaws_closed_event = threading.Event()
jaws_opened_event = threading.Event()
object_grasped_event = threading.Event()

OPEN_FLAG = 0b0
OLD_OPEN_FLAG = 0b0
CLOSED_FLAG = 0b0
OLD_CLOSED_FLAG = 0b0
HOLDING_FLAG = 0b0
OLD_HOLDING_FLAG = 0b0
FAULT_FLAG = 0b0

class serial_port_reader(threading.Thread):
	def __init__(self, serial_port):
		threading.Thread.__init__(self)
		self.serial_port = serial_port

	def extract_info(self, read_data_hexstr):
		status_flags_cond_var.acquire()
		global OPEN_FLAG 
		global OLD_OPEN_FLAG
		global CLOSED_FLAG 
		global OLD_CLOSED_FLAG 
		global HOLDING_FLAG
		global OLD_HOLDING_FLAG 
		global FAULT_FLAG
		#the data read from the serial port is @PDIN=[BYTE0,BYTE1,BYTE2,BYTE3] (see pag.20 in user manual)
		position_hexstr = read_data_hexstr[7:9] + read_data_hexstr[10:12] #remove the comma "," bewteen "BYTE0" and "BYTE1"
		POS = int(position_hexstr, 16)

		byte3_hexstr = read_data_hexstr[16:18]
		byte3_binary = int(byte3_hexstr, 16)
		mask = 0b1
		IDLE_FLAG = byte3_binary & mask

		byte3_binary = byte3_binary >> 1
		OLD_OPEN_FLAG = OPEN_FLAG
		OPEN_FLAG = byte3_binary & mask
		if OLD_OPEN_FLAG == 0 and OPEN_FLAG == 1:
			#the transition from jaws not opened to jaws opened has occured. Signal this event.
			jaws_opened_event.set()

		byte3_binary = byte3_binary >> 1
		OLD_CLOSED_FLAG = CLOSED_FLAG
		CLOSED_FLAG = byte3_binary & mask
		if OLD_CLOSED_FLAG == 0 and CLOSED_FLAG == 1:
			#the transition from jaws not closed to jaws closed has occured. Signal this event.
			jaws_closed_event.set()

		byte3_binary = byte3_binary >> 1
		OLD_HOLDING_FLAG = HOLDING_FLAG
		HOLDING_FLAG = byte3_binary & mask
		if OLD_HOLDING_FLAG == 0 and HOLDING_FLAG == 1:
			#the transition from not holding/grasping an object to holding/grasping an object has occured. Signal this event.
			object_grasped_event.set()

		byte3_binary = byte3_binary >> 1
		FAULT_FLAG = byte3_binary & mask

		byte3_binary = byte3_binary >> 1
		TEMPFAULT_FLAG = byte3_binary & mask

		byte3_binary = byte3_binary >> 1
		TEMPWARN_FLAG = byte3_binary & mask

		byte3_binary = byte3_binary >> 1
		MAINT_FLAG = byte3_binary & mask
		
		#print "POS = " + str(POS)
		#print "IDLE_FLAG = " + str(IDLE_FLAG)
		#print "OPEN_FLAG = " + str(OPEN_FLAG)
		#print "CLOSED_FLAG = " + str(CLOSED_FLAG)
		#print "HOLDING_FLAG = " + str(HOLDING_FLAG)
		#print "FAULT_FLAG = " + str(FAULT_FLAG)
		#print "TEMPFAULT_FLAG = " + str(TEMPFAULT_FLAG)
		#print "TEMPWARN_FLAG = " + str(TEMPWARN_FLAG)
		#print "MAINT_FLAG = " + str(MAINT_FLAG)

		status_flags_cond_var.notifyAll()
		status_flags_cond_var.release()

	def run(self):
		#read from port
		while True:
			serial_port_lock.acquire()
			try:
				if self.serial_port.isOpen():
					incoming_bytes_no = self.serial_port.inWaiting()
					if (incoming_bytes_no>0): #if incoming bytes are waiting to be read from the serial input buffer
						input_data = self.serial_port.read(self.serial_port.inWaiting())
						data_str = input_data.decode('ascii') #read the bytes and convert from binary array to ASCII
						if incoming_bytes_no == 22:
							self.extract_info(data_str)
						#print("incoming_bytes_no = " + str(incoming_bytes_no) + ": " + data_str)
			except Exception as e:
				print "error reading from the serial port: " + str(e)
			finally:
				serial_port_lock.release()

class weiss_gripper_ieg76(object):
	def __init__(self):
		jaws_closed_event.clear()
		jaws_opened_event.clear()
		object_grasped_event.clear()
		rospy.init_node('weiss_gripper_ieg76_node')
		serial_port_addr = rospy.get_param("~serial_port_address", '/dev/ttyACM0')
		self.ser = serial.Serial()
		self.ser.port = serial_port_addr
		self.ser.timeout = 0
		is_serial_port_opened = False
		while not is_serial_port_opened:
			try: 
				self.ser.open()
				is_serial_port_opened = True
			except Exception as e:
				is_serial_port_opened = False
				print "\terror opening serial port " + serial_port_addr + ": " + str(e)
				print "Retrying to open the serial port " + serial_port_addr + "..."
				time.sleep(1)
		
		print "Serial port opened: " + str(self.ser.isOpen())

		serv_ref = rospy.Service('reference', Empty, self.handle_reference)
		serv_open = rospy.Service('open_jaws', Trigger, self.handle_open_jaws)
		serv_close = rospy.Service('close_jaws', Trigger, self.handle_close_jaws)
		serv_grasp = rospy.Service('grasp_object', Trigger, self.handle_grasp_object)
		serv_close_port = rospy.Service('close_port', Trigger, self.handle_close_port)

		self.serial_port_reader_thread = serial_port_reader(self.ser)

		self.initialize_gripper()

		print "Ready to receive requests."

	def initialize_gripper(self):
		print("Query...")
		payload = struct.pack(">BBBB", 0x49, 0x44, 0x3f, 0x0a)
		serial_port_lock.acquire()
		try:
			self.ser.write(payload)
			time.sleep(0.5)
			print("PDOUT=[03,00] activate...")
			payload = struct.pack('>BBBBBBBBBBBBBB', 0x50, 0x44, 0x4f, 0x55, 0x54, 0x3d, 0x5b, 0x30, 0x33, 0x2c, 0x30, 0x30, 0x5d, 0x0a)
			self.ser.write(payload)
			time.sleep(0.5)
			print("Operate...")
			payload = struct.pack(">BBBBBBBBBB", 0x4f, 0x50, 0x45, 0x52, 0x41, 0x54, 0x45, 0x28, 0x29, 0x0a)
			self.ser.write(payload)
			time.sleep(0.5)
		except Exception as e:
			print "error reading from the serial port: " + str(e)
		finally:
			serial_port_lock.release()

	def handle_reference(self, req):
		print("PDOUT=[07,00] reference:")
		payload = struct.pack('>BBBBBBBBBBBBBB', 0x50, 0x44, 0x4f, 0x55, 0x54, 0x3d, 0x5b, 0x30, 0x37, 0x2c, 0x30, 0x30, 0x5d, 0x0a)
		self.ser.write(payload)
		return EmptyResponse()

	def handle_open_jaws(self, req):
		global OPEN_FLAG
		global CLOSED_FLAG
		print("Sending PDOUT=[02,00] to open the jaws...")
		payload = struct.pack('>BBBBBBBBBBBBBB', 0x50, 0x44, 0x4f, 0x55, 0x54, 0x3d, 0x5b, 0x30, 0x32, 0x2c, 0x30, 0x30, 0x5d, 0x0a)
		res = TriggerResponse()

		print "OPEN_FLAG = " + str(OPEN_FLAG)
		print "CLOSED_FLAG = " + str(CLOSED_FLAG)

		try:
			serial_port_lock.acquire()
			self.ser.write(payload)
		except SerialException as e:
			print "Error writing to the serial port: " + str(e)	
		finally:
			serial_port_lock.release()	
		
		if OPEN_FLAG == 0:
			open_jaws_timed_out = not(jaws_opened_event.wait(timeout=3.0))
			if open_jaws_timed_out:
				print "Timed out while trying to open the jaws."
				res.success = False
				res.message = "Timed out while trying to open the jaws."
			else:
				print "Opened the jaws."
				res.success = True
				res.message = "Jaws opened."
		else:
			print "The jaws are already opened."
			res.success = True
			res.message = "The jaws are already opened."

		print "OPEN_FLAG = " + str(OPEN_FLAG)
		print "CLOSED_FLAG = " + str(CLOSED_FLAG)	

		return res

	def handle_close_jaws(self, req):
		global OPEN_FLAG
		global CLOSED_FLAG
		global HOLDING_FLAG
		global FAULT_FLAG
		print "Sending PDOUT=[03,00] to completly close the jaws..."
		payload = struct.pack('>BBBBBBBBBBBBBB', 0x50, 0x44, 0x4f, 0x55, 0x54, 0x3d, 0x5b, 0x30, 0x33, 0x2c, 0x30, 0x30, 0x5d, 0x0a)
		res = TriggerResponse()

		print "OPEN_FLAG = " + str(OPEN_FLAG)
		print "CLOSED_FLAG = " + str(CLOSED_FLAG)
		print "HOLDING_FLAG = " + str(HOLDING_FLAG)
		print "FAULT_FLAG = " + str(FAULT_FLAG)

		try:
			serial_port_lock.acquire()
			self.ser.write(payload)
		except SerialException as e:
			print "Error writing to the serial port: " + str(e)	
		finally:
			serial_port_lock.release()	
		
		if CLOSED_FLAG == 0:
			close_jaws_timed_out = not(jaws_closed_event.wait(timeout=3.0))
			if close_jaws_timed_out:
				print "Timed out while trying to completly close the jaws."
				res.success = False
				res.message = "Timed out while trying to completly close the jaws."
				if HOLDING_FLAG == 1:
					print "Remove the object which is blocking the claws from completly closing and try again."
					res.message = res.message + " Remove the object which is blocking the claws from completly closing and try again."
			else:
				print "Completly closed the jaws."
				res.success = True
				res.message = "Jaws completly closed."
		else:
			print "The jaws are already completly closed."
			res.success = True
			res.message = "The jaws are already completly closed."
		
		print "OPEN_FLAG = " + str(OPEN_FLAG)
		print "CLOSED_FLAG = " + str(CLOSED_FLAG)
		print "HOLDING_FLAG = " + str(HOLDING_FLAG)
		print "FAULT_FLAG = " + str(FAULT_FLAG)

		return res

	def handle_grasp_object(self, req):
		global OPEN_FLAG
		global CLOSED_FLAG
		global HOLDING_FLAG
		global FAULT_FLAG
		print "Sending PDOUT=[03,00] to grasp an object..."
		payload = struct.pack('>BBBBBBBBBBBBBB', 0x50, 0x44, 0x4f, 0x55, 0x54, 0x3d, 0x5b, 0x30, 0x33, 0x2c, 0x30, 0x30, 0x5d, 0x0a)
		res = TriggerResponse()

		print "OPEN_FLAG = " + str(OPEN_FLAG)
		print "CLOSED_FLAG = " + str(CLOSED_FLAG)
		print "HOLDING_FLAG = " + str(HOLDING_FLAG)
		print "FAULT_FLAG = " + str(FAULT_FLAG)

		try:
			serial_port_lock.acquire()
			self.ser.write(payload)
		except SerialException as e:
			print "Error writing to the serial port: " + str(e)	
		finally:
			serial_port_lock.release()	
		
		if HOLDING_FLAG == 0:
			grasp_object_timed_out = not(object_grasped_event.wait(timeout=3.0))
			if grasp_object_timed_out:
				print "Timed out while trying to grasp an object."
				res.success = False
				res.message = "Timed out while trying to grasp an object."
				if CLOSED_FLAG == 1:
					print "No object to grasp."
					res.message = res.message + " No object to grasp."
			else:
				print "Grasped an object."
				res.success = True
				res.message = "Grasped an object."
		else:
			print "The jaws are already holding an object."
			res.success = True
			res.message = "The jaws are already holding an object."
		
		print "OPEN_FLAG = " + str(OPEN_FLAG)
		print "CLOSED_FLAG = " + str(CLOSED_FLAG)
		print "HOLDING_FLAG = " + str(HOLDING_FLAG)
		print "FAULT_FLAG = " + str(FAULT_FLAG)

		return res

	def handle_close_port(self, req):
		print "Closing the serial port " + self.ser.port + "..."
		res = TriggerResponse()
		serial_port_lock.acquire()
		try: 
			if self.ser.isOpen():
				self.ser.close()
				res.success = True
				res.message = "Closed the serial port " + self.ser.port
				print "Closed the serial port " + self.ser.port
		except SerialException as e:
			print "Error closing the serial port: " + str(e)
			res.success = False
			res.message = "Error closing the serial port " + self.ser.port + ": " + str(e)
		finally:
			serial_port_lock.release()
		return res

	def run(self):
		self.serial_port_reader_thread.daemon = True
		self.serial_port_reader_thread.start()
		
		rospy.spin()

if __name__ == "__main__":
	driver = weiss_gripper_ieg76()
	driver.run()