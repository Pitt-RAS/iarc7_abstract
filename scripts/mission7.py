#! /usr/bin/env python
import sys
import rospy
import math
import numpy as np

import actionlib
from iarc7_msgs.msg import BoolStamped
from iarc7_motion.msg import QuadMoveGoal, QuadMoveAction
from iarc7_safety.SafetyClient import SafetyClient
from iarc7_msgs.msg import OdometryArray
from nav_msgs.msg import Odometry
from actionlib_msgs.msg import GoalStatus

from iarc7_abstract.arena_position_estimator import ArenaPositionEstimator

from tf.transformations import euler_from_quaternion

TRANSLATION_HEIGHT = 1.5
MIN_GOTO_DISTANCE = 0.5
USE_PLANNER = True

TARGET_NUM_ROOMBAS = 2
MAX_FLIGHT_DURATION = 1 * 60

# Used if the planner is disabled
TRANSLATION_VELOCITY = 4.0

SEARCH_POINTS = np.asarray(
(
    ( 0.0,  0.0),   # Center
    ( 0.0,  7.5),   # Mid left
    (-7.5,  7.5),   # Bottom left
    (-7.5, -7.5),   # Bottom right
    ( 0.0, -7.5),   # Mid right
    ( 7.5,  0.0),   # Top middle
))

def target_roomba_law(roombas, odom):
    # Sort roombas by their distance to the drone
    sorted_roombas = sorted([(roomba_distance(r, odom), r) for r in roombas])
    return sorted_roombas[0][1]

def construct_velocity_goal(arena_pos, quad_odom, height=TRANSLATION_HEIGHT):
    map_pos = arena_position_estimator.arena_to_map(arena_pos)
    diff_x = arena_pos[0] - quad_odom.pose.pose.position.x
    diff_y = arena_pos[1] - quad_odom.pose.pose.position.y
    hypot = math.sqrt(diff_x**2 + diff_y**2)
    u_x = diff_x / hypot
    u_y = diff_y / hypot
    v_x = u_x * TRANSLATION_VELOCITY
    v_y = u_y * TRANSLATION_VELOCITY
    time = rospy.Duration(hypot / TRANSLATION_VELOCITY)
    return QuadMoveGoal(movement_type="velocity_test",
                        x_velocity=v_x,
                        y_velocity=v_y,
                        z_position=height,
                        time_velocity=time)


def construct_xyz_goal(arena_pos, height=TRANSLATION_HEIGHT):
    map_pos = arena_position_estimator.arena_to_map(arena_pos)
    return QuadMoveGoal(movement_type="xyztranslate", x_position=map_pos[0], y_position=map_pos[1], z_position=height)

def roomba_distance(roomba_odom, drone_odom):
    x_diff = drone_odom.pose.pose.position.x - roomba_odom.pose.pose.position.x
    y_diff = drone_odom.pose.pose.position.y - roomba_odom.pose.pose.position.y
    return math.sqrt(x_diff**2 + y_diff**2)

def find_roomba_by_id(roombas, desired_id):
    roomba_list = [x for x in roombas if x.child_frame_id == desired_id]
    if len(roomba_list) == 0:
        return None
    return roomba_list[0]

def roomba_yaw(roomba):
    orientation_list = [roomba.pose.pose.orientation.x,
                        roomba.pose.pose.orientation.y,
                        roomba.pose.pose.orientation.z,
                        roomba.pose.pose.orientation.w]

    angles = euler_from_quaternion(orientation_list)
    return angles[2]

def construct_goto_roomba_goal(roomba):
    return QuadMoveGoal(movement_type="xyztranslate",
                        x_position = roomba.pose.pose.position.x,
                        y_position = roomba.pose.pose.position.y,
                        z_position = TRANSLATION_HEIGHT)

def did_task_finish(client):
    return did_task_fail(client) or did_task_succeed(client)

def did_task_fail(client, state=None):
    if state is None:
        state = client.get_state()
    return (state == GoalStatus.ABORTED
            or state == GoalStatus.REJECTED
            or state == GoalStatus.PREEMPTED
            or state == GoalStatus.RECALLED)

def did_task_succeed(client, state=None):
    if state is None:
        state = client.get_state()
    return (state == GoalStatus.SUCCEEDED)

class Mission7(object):
    def __init__(self):
        self.safety_client = SafetyClient('mission7')
        # Since this abstract is top level in the control chain there is no need to check
        # for a safety state. We can also get away with not checking for a fatal state since
        # all nodes below will shut down.
        assert(self.safety_client.form_bond())
        if rospy.is_shutdown(): return

        # Creates the SimpleActionClient, passing the type of the action
        # (QuadMoveAction) to the constructor. (Look in the action folder)
        self._client = actionlib.SimpleActionClient("motion_planner_server", QuadMoveAction)

        # Waits until the action server has started up and started
        # listening for goals.
        self._client.wait_for_server()
        if rospy.is_shutdown(): return

        self._avail_roomba = None
        self._search_state = 0
        self._roomba_sub = rospy.Subscriber('/roombas', OdometryArray, self.roomba_callback)
        self._odom_sub = rospy.Subscriber('/odometry/filtered/', Odometry, self.odom_callback)

    def roomba_callback(self, data):
        self._avail_roombas = data.data

    def odom_callback(self, data):
        self._odom = data

    def begin_translate(self, arena_pos, height=TRANSLATION_HEIGHT):
        if USE_PLANNER:
            map_pos = construct_xyz_goal(arena_pos, height=height)
            self._client.send_goal(map_pos)
        else:
            self._client.send_goal(construct_velocity_goal(arena_pos, self._odom, height=height))

    def basic_goal(self, goal):
        self._client.send_goal(QuadMoveGoal(movement_type=goal))
        self._client.wait_for_result()
        rospy.loginfo('{} success: {}'.format(goal, self._client.get_result()))

    def wait_for_roomba(self):
        rate = rospy.Rate(30)
        while True:
            if did_task_fail(self._client):
                return (False, None)
            elif did_task_succeed(self._client):
                return (True, None)
            if self._avail_roombas is not None and len(self._avail_roombas) > 0:
                target_roomba = target_roomba_law(self._avail_roombas, self._odom)
                if target_roomba is not None:
                    return (True, target_roomba)
            rate.sleep()

    def search_for_roomba(self):
        rospy.loginfo('Entering search')
        # Decide which waypoint to go to
        if self._search_state != 0:
            self._search_state = 1

        # Execute search waypoints
        while True:
            self.begin_translate(SEARCH_POINTS[self._search_state])
            (xyz_translate_ok, roomba) = self.wait_for_roomba()
            if not xyz_translate_ok:
                continue

            if roomba is None:
                self._search_state = self._search_state + 1 if self._search_state + 1 < SEARCH_POINTS.shape[0] else 1
            else:
                break

        # Todo better initial search position
        self._search_state = 1
        return roomba

    def track_roomba_to_completion(self, roomba):
        track_state = 0
        roomba_id = roomba.child_frame_id
        rospy.loginfo('Entering track roomba: {}'.format(roomba_id))

        rate = rospy.Rate(30)
        while True:
            roomba = find_roomba_by_id(self._avail_roombas, roomba_id)
            if roomba is None:
                rospy.loginfo('Roomba no longer is tracked {}'.format(roomba_id))
                return False

            d = roomba_distance(roomba, self._odom)

            # Need to decide what kind of positional action to take
            if track_state == 0:

                # Regardless of the positional action necessary, make sure the last task is canceled

                if d >= MIN_GOTO_DISTANCE:
                    self._client.send_goal(construct_goto_roomba_goal(roomba))
                    rospy.loginfo('GOTO ROOMBA')
                    track_state = 1
                else:
                    self._client.send_goal(QuadMoveGoal(movement_type="track_roomba",
                                                        frame_id=roomba_id,
                                                        x_overshoot=0.0, y_overshoot=0.0))
                    rospy.loginfo('TRACKING ROOMBA')
                    track_state = 2

            # Going to roomba
            if track_state == 1:
                state = self._client.get_state()
                if did_task_succeed(self._client):
                    rospy.loginfo('GOTO ROOMBA SUCCESS')
                    track_state = 0
                if did_task_fail(self._client):
                    rospy.loginfo('GOTO ROOMBA FAILED')
                    track_state = 0

                if d < MIN_GOTO_DISTANCE:
                    self._client.cancel_goal()
                    self._client.send_goal(QuadMoveGoal(movement_type="track_roomba",
                                                        frame_id=roomba_id,
                                                        x_overshoot=0.0, y_overshoot=0.0, time_to_track=100000.0))
                    rospy.loginfo('TRACKING ROOMBA')
                    track_state = 2

            if track_state == 2:
                state = self._client.get_state()
                if did_task_finish(self._client):
                    rospy.loginfo('Track roomba kicked back')
                    track_state = 0
                else:
                    roomba_heading = roomba_yaw(roomba)

            rate.sleep()

    def attempt_mission7(self):
        # Takeoff

        flight_start_time = rospy.Time.now()

        self.basic_goal('takeoff')

        mission7_completed = False
        gotten_roombas = 0
        while not mission7_completed:
            roomba = self.search_for_roomba()

            got_roomba = self.track_roomba_to_completion(roomba)
            if got_roomba:
                gotten_roombas += 1

            if gotten_roombas > TARGET_NUM_ROOMBAS:
                break

            if rospy.Time.now() > flight_start_time + rospy.Duration(MAX_FLIGHT_DURATION):
                break

        # self.goto_safe_landing_spot()
        self.basic_goal('land')

if __name__ == '__main__':
    # Initializes a rospy node so that the SimpleActionClient can
    # publish and subscribe over ROS.
    rospy.init_node('mission7')
    arena_position_estimator = ArenaPositionEstimator()

    mission7 = Mission7()

    pub = rospy.Publisher('/start_roombas', BoolStamped, queue_size=100, latch=True)
    msg = BoolStamped()
    msg.header.stamp = rospy.Time.now()
    pub.publish(msg)
    mission7.attempt_mission7()

    rospy.spin()