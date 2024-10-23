import datetime
import logging
import os
import time
from dataclasses import dataclass
from glob import glob
from pathlib import Path

import h5py
import hydra
import numpy as np
from omegaconf import DictConfig, OmegaConf

from silverscreen.filters import LPRotationFilter
from silverscreen.player import TeleopRobot
from silverscreen.state_machine import FSM
from silverscreen.utils import CONFIG_DIR, RECORD_DIR, KeyboardListener

logger = logging.getLogger(__name__)
OmegaConf.register_new_resolver("eval", eval, replace=True)

np.set_printoptions(precision=2, suppress=True)


class InitializationError(Exception):
    pass


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def get_episode_id(session_path: str) -> int:
    """glob existing episodes and extract their IDs, and return the next episode ID"""
    episodes = glob(f"{session_path}/*.hdf5")
    if not episodes:
        return 0
    return max([int(ep.split("_")[-1].split(".")[0]) for ep in episodes]) + 1


@dataclass
class RecordingInfo:
    episode_id: int
    session_path: str
    episode_path: str
    video_path: str

    @classmethod
    def from_session_path(cls, session_path: str):
        episode_id = get_episode_id(session_path)
        episode_path = os.path.join(session_path, f"episode_{episode_id:06d}.hdf5")
        video_path = os.path.join(session_path, f"episode_{episode_id:06d}")
        return cls(episode_id, session_path, episode_path, video_path)

    def __post_init__(self):
        os.makedirs(
            self.session_path,
            exist_ok=True,
        )
        # os.makedirs(
        #     self.images_path,
        #     exist_ok=True,
        # )

    def increment(self):
        self.episode_id += 1
        self.episode_path = os.path.join(self.session_path, f"episode_{self.episode_id:06d}.hdf5")
        self.video_path = os.path.join(self.session_path, f"episode_{self.episode_id:06d}")


def make_data_dict():
    # TODO: make this configurable
    camera_names = ["left", "right"]
    depth_camera_names = ["left"]
    data_dict = {
        "timestamp": [],
        "obs": {"qpos": [], "hand_qpos": [], "ee_pose": [], "head_pose": []},
        "action": {
            "joints": [],
            "hands": [],
            "ee_pose": [],
        },
    }

    for cam in camera_names:
        data_dict["obs"][f"camera_{cam}"] = []

    for cam in depth_camera_names:
        data_dict["obs"][f"depth_{cam}"] = []

    return data_dict


@hydra.main(config_path=str(CONFIG_DIR), config_name="teleop_gr1", version_base="1.2")
def main(
    cfg: DictConfig,
):
    if not cfg.use_waist:
        cfg.robot.joints_to_lock.extend(cfg.robot.waist_joints)

    if not cfg.use_head:
        cfg.robot.joints_to_lock.extend(cfg.robot.head_joints)

    recording = None
    data_dict = make_data_dict()

    session_name = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    if cfg.recording.enabled:
        session_path = RECORD_DIR / cfg.recording.task_name / session_name
        logger.info(f"Recording session: {session_path}")
        os.makedirs(session_path, exist_ok=True)

        recording = RecordingInfo.from_session_path(str(session_path))

    fsm = FSM()

    act = False

    robot = TeleopRobot(cfg)  # type: ignore

    listener = KeyboardListener()
    listener.start()

    def trigger():
        return listener.space_pressed

    head_filter = LPRotationFilter(cfg.head_filter.alpha)

    logger.info("Waiting for connection.")

    start_timer = None

    collection_start = None
    i = 0
    try:
        while True:
            start = time.time()
            # ----- update readings -----
            (
                head_mat,
                left_pose,
                right_pose,
                left_qpos,
                right_qpos,
            ) = robot.step()

            if fsm.state == FSM.State.COLLECTING or not cfg.recording.enabled:
                timestamp = robot.update_image()
            else:
                timestamp = robot.update_image(marker=True)

            head_mat = head_filter.next_mat(head_mat)

            robot.solve(left_pose, right_pose, head_mat, dt=1 / cfg.frequency)

            robot.set_hand_joints(left_qpos, right_qpos)

            if robot.viz:
                robot.viz.viewer["head"].set_transform(head_mat)

            robot.update_display()

            # ----- logic -----
            if not robot.tv.connected:
                continue
            if robot.tv.connected and fsm.state == FSM.State.INITIALIZED:
                logger.info("Connected to headset.")
                fsm.next()
                start_timer = time.time()

            if start_timer is None:
                raise InitializationError("start_timer is None")

            if time.time() - start_timer < cfg.wait_time:
                logger.info(f"Waiting for trigger. Time elapsed: {time.time() - start_timer:.2f}")

            if fsm.state == FSM.State.STARTED and time.time() - start_timer > cfg.wait_time and trigger():
                logger.info("Trigger detected")

                fsm.next()
            elif fsm.state == FSM.State.CALIBRATING:
                logger.info("Calibrating.")
                # TODO: average over multiple frames
                robot.processor.calibrate(robot, head_mat, left_pose, right_pose)
                fsm.next()
            elif fsm.state == FSM.State.CALIBRATED:
                act = True
                fsm.next()
            elif fsm.state == FSM.State.ENGAGED and trigger():
                if not cfg.recording.enabled or recording is None:
                    logger.info("Disengaging.")
                    fsm.state = FSM.State.IDLE
                    robot.pause_robot()
                    continue
                fsm.next()

            elif fsm.state == FSM.State.IDLE and trigger():
                if not cfg.recording.enabled or recording is None:
                    logger.info("Engaging.")
                    fsm.state = FSM.State.ENGAGED
                    continue
                logger.info("Starting new episode.")
                fsm.next()

            elif fsm.state == FSM.State.EPISODE_STARTED:
                if not cfg.recording.enabled or recording is None:
                    raise InitializationError("Recording not initialized.")
                collection_start = time.time()

                robot.start_recording(str(recording.video_path))

                data_dict = make_data_dict()

                logger.info(f"Episode {recording.episode_id} started.")
                fsm.next()
                continue
            elif fsm.state == FSM.State.COLLECTING and trigger():
                fsm.next()

            elif fsm.state == FSM.State.EPISODE_ENDED:
                if not cfg.recording.enabled or recording is None:
                    raise InitializationError("Recording not initialized.")
                robot.pause_robot()

                episode_length = time.time() - collection_start  # type: ignore

                logger.info(
                    f"Episode {recording.episode_id} took {episode_length:.2f} seconds. Saving data to {recording.episode_path}"
                )

                # check for inhomogeneous data
                try:
                    with h5py.File(recording.episode_path, "w", rdcc_nbytes=1024**2 * 2) as f:
                        obs = f.create_group("obs")
                        action = f.create_group("action")

                        f.create_dataset("timestamp", data=data_dict["timestamp"])

                        for name in data_dict["obs"]:
                            obs.create_dataset(name, data=data_dict["obs"][name])

                        for name in data_dict["action"]:
                            action.create_dataset(name, data=data_dict["action"][name])

                except Exception as e:
                    logger.error(f"Error saving episode: {e}")
                    import pickle

                    pickle.dump(data_dict, open(recording.episode_path + ".pkl", "wb"))
                    exit(1)

                recording.increment()
                robot.stop_recording()

                fsm.next()
                continue

            if act and (
                fsm.state == FSM.State.ENGAGED
                or fsm.state == FSM.State.EPISODE_STARTED
                or fsm.state == FSM.State.COLLECTING
            ):
                if not cfg.sim:
                    filtered_hand_qpos = robot.control_hands(left_qpos, right_qpos)
                    qpos = robot.control_joints(gravity_compensation=True)  # TODO: add gravity compensation

                    if fsm.state == FSM.State.COLLECTING:
                        data_dict["action"]["hands"].append(filtered_hand_qpos)
                        data_dict["action"]["joints"].append(qpos)
                        data_dict["action"]["ee_pose"].append(np.hstack([left_pose, right_pose]))

            if fsm.state == FSM.State.COLLECTING:
                data_dict["timestamp"].append(timestamp)

                qpos, hand_qpos, ee_pose, head_pose = robot.observe()

                data_dict["obs"]["qpos"].append(qpos)
                data_dict["obs"]["hand_qpos"].append(hand_qpos)
                data_dict["obs"]["ee_pose"].append(ee_pose)
                data_dict["obs"]["head_pose"].append(head_pose)

                # for cam in camera_names:
                #     data_dict["obs"][f"camera_{cam}"].append(i)

                i += 1

                # print("--------------------")
                # # print(f"head_euler: {np.rad2deg(head_euler)}")
                # print(f"head_trans: {head_mat[:3, 3]}")
                # print(f"left_pose: {left_pose}")
                # print(f"right_pose: {right_pose}")
                # print(f"left_qpos: {left_qpos}")
                # print(f"right_qpos: {right_qpos}")
                # print("--------------------")
                # print(data_dict)

            exec_time = time.time() - start
            # print(f"Execution time: {1/exec_time:.2f} hz")
            # print(max(0, 1 / config.frequency - exec_time))
            time.sleep(max(0, 1 / cfg.frequency - exec_time))

    except KeyboardInterrupt:
        robot.stop_recording()
        robot.end()

        time.sleep(1.0)
        exit(0)


if __name__ == "__main__":
    main()
