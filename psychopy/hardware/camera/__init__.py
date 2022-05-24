#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Classes and functions for reading and writing camera streams.

A camera may be used to document participant responses on video or used by the
experimenter to create movie stimuli or instructions.

"""

# Part of the PsychoPy library
# Copyright (C) 2002-2018 Jonathan Peirce (C) 2019-2022 Open Science Tools Ltd.
# Distributed under the terms of the GNU General Public License (GPL).

__all__ = ['CameraNotFoundError', 'Camera', 'CameraInfo', 'getCameras']

import glob
import platform
import numpy as np
import tempfile
import os
from psychopy.constants import STOPPED, STOPPING, NOT_STARTED, RECORDING
from psychopy.core import getTime
from psychopy.visual.movies.metadata import MovieMetadata, NULL_MOVIE_METADATA
from psychopy.visual.movies.frame import MovieFrame, NULL_MOVIE_FRAME_INFO
from ffpyplayer.player import MediaPlayer
from ffpyplayer.writer import MediaWriter
from ffpyplayer.pic import SWScale
from ffpyplayer.tools import list_dshow_devices
from moviepy.editor import VideoFileClip, AudioFileClip, CompositeAudioClip
import uuid
from psychopy.preferences import prefs


# ------------------------------------------------------------------------------
# Constants
#

VIDEO_DEVICE_ROOT_LINUX = '/dev'
WEBCAM_UNKNOWN_VALUE = u'Unknown'  # fields where we couldn't get a value
WEBCAM_NULL_VALUE = u'Null'  # fields where we couldn't get a value

# camera operating modes
WEBCAM_MODE_VIDEO = u'video'
WEBCAM_MODE_CV = u'cv'
WEBCAM_MODE_PHOTO = u'photo'


# ------------------------------------------------------------------------------
# Exceptions
#

class CameraError(Exception):
    """Base class for errors around the camera."""


class CameraNotReadyError(CameraError):
    """Camera is not ready."""


class CameraNotFoundError(CameraError):
    """Raised when a camera cannot be found on the system."""


class PlayerNotAvailableError(Exception):
    """Raised when a player object is not available but is required."""


# ------------------------------------------------------------------------------
# Descriptors
#

class CameraInfo:
    """Descriptor for cameras connected to the system (such as webcams).

    Parameters
    ----------
    name : str
        Camera name retrieved by the OS. This may be a human-readable name
        (i.e. DirectShow on Windows) or a path (e.g., `/dev/video0` on Linux).
    frameSize : ArrayLike
        Resolution of the frame `(w, h)` in pixels.
    frameRateRange : ArrayLike
        Minimum and maximum frame rate supported by the camera at the specified
        color/pixel format and resolution.
    pixelFormat : str
        Pixel format for the stream. If `u'Null'`, then `codecFormat` is being
        used to configure the camera.
    codecFormat : str
        Codec format for the stream.  If `u'Null'`, then `pixelFormat` is being
        used to configure the camera. Usually this value is used for high-def
        stream formats.

    """
    __slots__ = [
        '_name',
        '_frameSize',
        '_frameRateRange',
        '_pixelFormat',
        '_codecFormat',
        '_cameraLib',
        '_cameraAPI'  # API in use, e.g. DirectShow on Windows
    ]

    def __init__(self,
                 name=WEBCAM_NULL_VALUE,
                 frameSize=(-1, -1),
                 frameRateRange=(-1, -1),
                 pixelFormat=WEBCAM_UNKNOWN_VALUE,
                 codecFormat=WEBCAM_UNKNOWN_VALUE,
                 cameraLib=WEBCAM_NULL_VALUE,
                 cameraAPI=WEBCAM_NULL_VALUE):

        self.name = name
        self.frameSize = frameSize
        self.frameRateRange = frameRateRange
        self.pixelFormat = pixelFormat
        self.codecFormat = codecFormat
        self._cameraLib = cameraLib
        self._cameraAPI = cameraAPI

    @property
    def name(self):
        """Camera name (`str`). This is the camera name retrieved by the OS.
        """
        return self._name

    @name.setter
    def name(self, value):
        self._name = str(value)

    @property
    def frameSize(self):
        """Resolution (w, h) in pixels (`ArrayLike`).
        """
        return self._frameSize

    @frameSize.setter
    def frameSize(self, value):
        assert len(value) == 2, "Value for `frameSize` must have length 2."
        assert all([isinstance(i, int) for i in value]), (
            "Values for `frameSize` must be integers.")

        self._frameSize = value

    @property
    def frameRateRange(self):
        """Resolution (min, max) in pixels (`ArrayLike`).
        """
        return self._frameRateRange

    @frameRateRange.setter
    def frameRateRange(self, value):
        assert len(value) == 2, "Value for `frameRateRange` must have length 2."
        assert all([isinstance(i, int) for i in value]), (
            "Values for `frameRateRange` must be integers.")
        assert value[0] <= value[1], (
            "Value for `frameRateRange` must be `min` <= `max`.")

        self._frameRateRange = value

    @property
    def pixelFormat(self):
        """Video pixel format (`str`). An empty string indicates this field is
        not initialized.
        """
        return self._pixelFormat

    @pixelFormat.setter
    def pixelFormat(self, value):
        self._pixelFormat = str(value)

    @property
    def codecFormat(self):
        """Codec format, may be used instead of `pixelFormat` for some
        configurations. Default is `''`.
        """
        return self._codecFormat

    @codecFormat.setter
    def codecFormat(self, value):
        self._codecFormat = str(value)

    def supportedFrameRate(self, frameRate):
        """Check if the specified frame rate is supported by the camera
        configuration.

        Parameter
        ---------
        frameRate : int or float
            Framerate in Hertz (Hz).

        Returns
        -------
        bool
            `True` if the specified framerate is supported by the camera.

        """
        frameRateMin, frameRateMax = self._frameRateRange

        return frameRateMin <= frameRate <= frameRateMax


# ------------------------------------------------------------------------------
# Classes
#


class Camera:
    """Class of displaying and recording video from a USB/PCI connected camera
    (usually a camera).

    This class is capable of opening and recording camera video streams.

    Parameters
    ----------
    device : str or int
        Camera to open a stream with. If the ID is not valid, an error will be
        raised when `start()` is called. Value can be a string or number. String
        values are platform-dependent: a DirectShow URI on Windows, a path
        on GNU/Linux (e.g., `'/dev/video0'`), and a camera name on MacOS.
        Specifying a number (>=0) is a platform-independent means of selecting a
        camera. PsychoPy enumerates possible camera devices and makes them
        selectable without explicitly having the name of the cameras attached to
        the system. Use caution when specifying a number, as the same index may
        not reference the same camera everytime.
    mic : :class:`~psychopy.sound.microphone.Microphone` or None
        Microphone to record audio samples from during recording. The microphone
        input device must not be in use when `record()` is called.
    mode : str
        Camera operating mode to use. Value can be either `'video'`, `'cv'` or
        `'photo'`. Use `'video'` for recording live-feeds to produce movies,
        `'cv'` for computer vision applications (same as `'video'` but frames
        are not buffered on disk, reduces CPU load), and `'photo'` for taking
        snapshots with the camera. Default operating mode is `'video'`, cannot
        be set after initialization.
    cameraLib : str
        Interface library (backend) to use for accessing the camera. Only
        `ffpyplayer` is available at this time.
    codecOpts : dict or None
        Options to pass to the codec. See the documentation for the camera
        library for details. Some options may be set by this class.
    libOpts : dict or None
        Additional options to configure the camera interface library (if
        applicable).

    Examples
    --------
    Opening a camera stream and closing it::

        camera = Webcam(camera='/dev/video0')
        camera.open()  # exception here on invalid camera
        # camera.status == NOT_STARTED
        camera.start()
        # camera.status == PLAYING
        camera.stop()
        # camera.status == STOPPED
        camera.close()

    """
    def __init__(self, device=0, mic=None, mode='video',
                 cameraLib=u'ffpyplayer', codecOpts=None, libOpts=None):

        # add attributes for setters
        self.__dict__.update(
            {'_device': None,
             '_mic': None,
             '_outFile': None,
             '_mode': u'video',
             '_cameraLib': u'',
             '_codecOpts': None,
             '_libOpts': None})

        # resolve getting the camera identifier
        if isinstance(device, int):  # get camera if integer
            try:
                self.device = getCameras()[device]
            except IndexError:  # catch as
                raise CameraNotFoundError(
                    'Could not enumerate camera with index `{}`.'.format(
                        device))
        elif isinstance(device, str):  # get camera if integer
            self.device = device
        else:
            raise TypeError(
                "Incorrect type for `camera`, expected `int` or `str`.")

        # camera library in use
        self._cameraLib = cameraLib

        # operating mode
        if mode not in (WEBCAM_MODE_VIDEO, WEBCAM_MODE_CV, WEBCAM_MODE_PHOTO):
            raise ValueError(
                "Invalid value for parameter `mode`, expected one of `'video'` "
                "`'cv'` or `'photo'`.")
        self._mode = mode

        # FFMPEG and FFPyPlayer options
        self._codecOpts = codecOpts if codecOpts is not None else {}
        self._libOpts = libOpts if libOpts is not None else {}

        # parameters for the writer
        self._writer = None
        self._tempVideoFileName = u''
        self._tempAudioFileName = u''
        self._tempRootDir = u'.'

        self.mic = mic

        # current camera frame since the start of recording
        self._player = None  # media player instance
        self._status = NOT_STARTED
        self._frameIndex = -1
        self._isRecording = False

        # timestamp data
        self._startPts = -1.0  # absolute stream time at recording
        self._absPts = -1.0  # timestamp of the video stream in absolute time
        self._pts = -1.0  # timestamp used for writing the video stream

        # video metadata
        self._recentMetadata = None

        # last frame
        self._lastFrame = NULL_MOVIE_FRAME_INFO

    def authorize(self):
        """Get permission to access the camera. Not implemented locally yet.
        """
        pass  # NOP

    @property
    def isReady(self):
        """Is the camera ready (`bool`)?

        The camera is ready when the following conditions are met. First, we've
        created a player interface and opened it. Second, we have received
        metadata about the stream. At this point we can assume that the camera
        is 'hot' and the stream is being read.

        """
        # The camera is ready when the following conditions are met. First,
        # we've created a player interface and opened it. Second, we have
        # received metadata about the stream. At this point we can assume that
        # the camera is 'hot' and the stream is being read.
        #
        hasPlayer = self._player is not None
        streamReady = self._recentMetadata is not NULL_MOVIE_METADATA

        return hasPlayer and streamReady

    def _assertCameraReady(self):
        """Assert that the camera is ready. Raises a `CameraNotReadyError` if
        the camera is not ready.
        """
        if not self.isReady:
            raise CameraNotReadyError("Camera is not ready.")

    @property
    def metadata(self):
        """Video metadata retrieved during the last frame update
        (`MovieMetadata`).
        """
        return self._recentMetadata

    def getMetadata(self):
        """Get stream metadata.

        Returns
        -------
        MovieMetadata
            Metadata about the video stream, retrieved during the last frame
            update (`_enqueueFrame` call).

        """
        return self._recentMetadata

    @property
    def mode(self):
        """Operating mode in use for this camera.
        """
        return self._mode

    @staticmethod
    def getWebcams():
        """Get information about installed cameras on this system.

        Returns
        -------
        list
            Camera identifiers.

        """
        return getCameras()

    def _openWriter(self):
        """Initialize and configure the media writer.

        Must be called after the video stream has been opened and
        `_enqueueFrame` called at least once prior.
        """
        if self._writer is not None:
            raise RuntimeError(
                "Stream writer instance has already been created.")

        self._assertMediaPlayer()

        if self._outFile is None:
            return  # nop if there is no output path

        # configure the temp directory and files for the recordings
        randFileName = str(uuid.uuid4().hex)
        self._tempRootDir = tempfile.mkdtemp(
            suffix=randFileName,
            prefix='psychopy-',
            dir=None)
        self._tempVideoFileName = os.path.join(
            self._tempRootDir,
            'video-' + randFileName + '.mp4')
        self._tempAudioFileName = os.path.join(
            self._tempRootDir,
            'audio-' + randFileName + '.wav')

        frameWidth, frameHeight = self._recentMetadata.size
        frameRate = self._recentMetadata.frameRate

        writerOptions = {
            'pix_fmt_in': 'yuv420p',  # default for now
            'width_in': frameWidth,
            'height_in': frameHeight,
            'frame_rate': frameRate
        }

        # initialize the writer to transcode the video stream to file
        self._writer = MediaWriter(self._tempVideoFileName, [writerOptions])

        # recording timestamp
        self._pts = -1.0

    def _closeWriter(self):
        """Close the video writer.
        """
        if self._writer is None:
            return

        # cleanup
        self._writer.close()
        self._writer = None

        # reset properties for a new recording
        self._startPts = self._pts = self._absPts = -1.0

    def _renderVideo(self):
        """Combine video and audio tracks of temporary video and audio files.
        Outputs a new file at `outFile` with merged video and audio tracks.
        """
        # do nothing if there is no output file
        if self._outFile is None:
            return False

        # this can only happen when stopped
        if self._status != STOPPED:
            raise RuntimeError(
                "Cannot render video, `stop` has not been called yet.")

        # merge audio and video tracks, we use MoviePy for this
        videoClip = VideoFileClip(self._tempVideoFileName)
        audioClip = AudioFileClip(self._tempAudioFileName)

        # add audio track to the video
        videoClip.audio = CompositeAudioClip([audioClip])
        videoClip.write_videofile(self._outFile)

        return True

    @property
    def status(self):
        """Status flag for the camera (`int`).

        Can be either `RECORDING`, `STOPPED`, `STOPPING`, or `NOT_STARTED`.

        """
        return self._status

    @property
    def outFile(self):
        """Output file for the video stream (`str`).
        """
        return self._outFile

    @outFile.setter
    def outFile(self, value):
        if self._writer is not None:
            raise ValueError("Cannot change `outFile` while recording.")

        self._outFile = value

    @property
    def device(self):
        """Camera to use (`str` or `None`).

        String specifying the name of the camera to open a stream with. This
        must be set prior to calling `start()`. If the name is not valid, an
        error will be raised when `start()` is called.

        """
        return self._device

    @device.setter
    def device(self, value):
        self._device = value

    @property
    def mic(self):
        """Microphone to record audio samples from during recording
        (:class:`~psychopy.sound.microphone.Microphone` or `None`). If `None`,
        no audio will be recorded.
        """
        return self._mic

    @mic.setter
    def mic(self, value):
        self._mic = value

    @property
    def _hasPlayer(self):
        """`True` if we have an active media player instance.
        """
        return self._player is not None

    @property
    def _hasWriter(self):
        """`True` if we have an active file writer instance.
        """
        return self._writer is not None

    @property
    def streamTime(self):
        """Current stream time in seconds (`float`). This time increases
        monotonically from startup.
        """
        return self._absPts

    @property
    def recordingTime(self):
        """Current recording timestamp (`float`).

        This value increases monotonically from the last `record()` call. It
        will reset once `stop()` is called. This value is invalid outside
        `record()` and `stop()` calls.

        """
        return self._pts

    def _assertMediaPlayer(self):
        """Assert that we have a media player instance open.

        This will raise a `RuntimeError` if there is no player open. Use this
        function to ensure that a player is present before running subsequent
        code.
        """
        if self._player is not None:
            return

        raise PlayerNotAvailableError('Media player not initialized.')

    def _writeFrame(self, colorData, timestamp):
        """Write the presently enqueued frame to the output file.

        Parameters
        ----------
        colorData : object
            Image frame to write.
        timestamp : float
            Timestamp of the frame in seconds.

        """
        if not self._hasWriter:  # NOP if no writer
            return

        if self._status != RECORDING:  # nop if not recording
            return

        # convert the image to the appropriate format for the encoder
        frameWidth, frameHeight = self._recentMetadata.size
        pixelFormat = self._recentMetadata.pixelFormat
        sws = SWScale(frameWidth, frameHeight, pixelFormat, ofmt='yuv420p')

        # write the frame to the file
        self._writer.write_frame(
            img=sws.scale(colorData),
            pts=timestamp,
            stream=0)

    def _enqueueFrame(self, timeout=1.0):
        """Grab the latest frame from the stream.

        Parameters
        ----------
        timeout : float
            Amount of time to wait for a frame in seconds. If -1.0, this method
            will return immediately. If a frame could not be pulled from the
            stream in the allotted time a warning will be logged.

        Returns
        -------
        bool
            `True` if a frame has been enqueued. Returns `False` if the camera
            is not ready or if the stream was closed.

        """
        self._assertMediaPlayer()

        # update metadata
        self._recentMetadata = self._player.get_metadata()

        # grab a frame from the camera
        frame = None
        status = ''
        timedOut = False
        tStart = getTime()
        while not timedOut:  # not frame available
            timedOut = (getTime() - tStart) < timeout
            frame, status = self._player.get_frame()
            # got a valid frame within the timeout period
            if frame is not None:
                break

        if timedOut:  # we timed out and don't have a frame
            return False

        # process the frame
        colorData, absPts = frame

        # compute timestamps if needed
        self._absPts = absPts
        if self._status == RECORDING:
            if self._lastFrame is NULL_MOVIE_FRAME_INFO:
                self._startPts = absPts

            # compute new recording timestamp
            self._pts = self._absPts - self._startPts
        else:
            # if not recording, return negative timestamp
            self._startPts = self._pts = -1.0

        # if we have a new frame, update the frame information
        videoBuffer = colorData.to_bytearray()[0]
        videoFrameArray = np.frombuffer(videoBuffer, dtype=np.uint8)

        # provide the last frame
        self._lastFrame = MovieFrame(
            frameIndex=self._frameIndex,
            absTime=self._absPts,
            # displayTime=self._recentMetadata['frame_size'],
            size=self._recentMetadata['src_vid_size'],
            colorData=videoFrameArray,
            audioChannels=0,
            audioSamples=None,
            movieLib=u'ffpyplayer',
            userData=None)

        # write the frame to the file
        self._writeFrame(colorData, self.recordingTime)

        if status == 'eof':  # end of stream but there is a valid frame
            self._status = STOPPING  # last frame, stopping ...

        return True

    def open(self):
        """Open the camera stream and begin decoding frames (if available).

        The value of `lastFrame` will be updated as new frames from the camera
        arrive.

        """
        if self._hasPlayer:
            raise RuntimeError('Cannot open `MediaPlayer`, already opened.')

        ff_opts = {}  # ffmpeg options
        lib_opts = {}  # ffpyplayer options
        if platform.system() == 'Windows':  # DirectShow specific stuff
            ff_opts['f'] = 'dshow'

            # library options
            framerate = str(30)
            videoSize = '{width}x{height}'.format(width=320, height=240)
            bufferSize = 320 * 240 * 4

            # build dict for library options
            lib_opts.update({
                'framerate': framerate,
                'video_size': videoSize,
                'pixel_format': 'yuyv422',
                'rtbufsize': str(bufferSize)}
            )
            _camera = 'video={}'.format(self._device)
        else:
            _camera = self._device

        # open a stream and pause it until ready
        self._player = MediaPlayer(_camera, ff_opts=ff_opts, lib_opts=lib_opts)
        self._enqueueFrame(timeout=1.0)  # pull a frame, gets metadata too

        self._openWriter()  # open the file for writing stream to

    def record(self, streamOnly=False):
        """Start recording frames.

        Parameters
        ----------
        streamOnly : bool
            Set as `True` to prevent writing any video stream data to disk. This
            can be used to reduce CPU load if the camera is being used for
            applications other than video recording (e.g., computer vision,
            etc.) Default value is `False`.

        """
        self._assertMediaPlayer()

        if not streamOnly:  # don't save anything to disk
            self._openWriter()

        self._lastFrame = NULL_MOVIE_FRAME_INFO

        self._status = RECORDING

        # start audio recording if possible
        if self._mic is not None:
            self._mic.record()

    def snapshot(self):
        """Take a photo with the camera. The camera must be in `'photo'` mode
        to use this method.
        """
        pass

    def stop(self):
        """Stop recording frames.
        """
        self._assertMediaPlayer()

        self._status = STOPPED
        self._player.close_player()

        self._closeWriter()  # close the writer

    def close(self):
        """Close the camera.
        """
        if not self._hasPlayer:
            raise RuntimeError("Cannot close stream, not opened yet.")

        self._player.close_player()
        self._player = None  # reset

        # close the file writer
        if self._writer is not None:
            self._writer.close()

    def save(self, filename):
        """Save the last recording to file.

        This will write the last video recording to `filename`. Method `stop()`
        must be called prior to saving a video. If `record()` is called again
        before `save()`, the previous recording will be deleted and lost.

        Returns
        -------
        int
            Size of the output file at `filename` in bytes.

        """
        if self._status != STOPPED:
            raise RuntimeError(
                "Attempted to call `save()` a file before calling `stop()`.")

        # render the video
        if not self._renderVideo():
            raise RuntimeError(
                "Failed to write file `filename`, check if the output path is "
                "writeable.")

        # make sure that `filename` is valid
        self._outFile = filename

        return os.path.getsize(self._outFile)

    def _upload(self):
        """Upload video file to an online repository. Not implemented locally,
        needed for auto translate to JS.
        """
        pass  # NOP

    def _download(self):
        """Download video file to an online repository. Not implemented locally,
        needed for auto translate to JS.
        """
        pass  # NOP

    @property
    def lastClip(self):
        """File path to the last recording (`str` or `None`).

        This value is only valid if a previous recording has been saved
        successfully (`save()` was called), otherwise it will be set to `None`.

        """
        return self._outFile  # change this to the actual value eventually

    @property
    def lastFrame(self):
        """Most recent frame pulled from the camera (`VideoFrame`) since the
        last call of `getVideoFrame`.
        """
        return self._lastFrame

    def update(self):
        """Acquire the newest data from the camera stream. If the `Camera`
        object is not being monitored by a `ImageStim`, this must be explicitly
        called.
        """
        pass

    def getVideoFrame(self, timeout=0.0):
        """Pull the next frame from the stream (if available).

        Returns
        -------
        MovieFrame
            Most recent video frame. Returns `NULL_MOVIE_FRAME_INFO` if no
            frame was available, or we timed out.

        """
        self._assertMediaPlayer()

        self._enqueueFrame(timeout=timeout)

        return self._lastFrame

    def __del__(self):
        """Try to cleanly close the camera and output file.
        """
        if hasattr(self, '_player'):
            if self._player is not None:
                try:
                    self._player.close_player()
                except AttributeError:
                    pass

        if hasattr(self, '_writer'):
            if self._writer is not None:
                try:
                    self._writer.close()
                except AttributeError:
                    pass


# ------------------------------------------------------------------------------
# Functions
#

def getCameras():
    """Get information about installed cameras on this system.

    Returns
    -------
    list
        Camera identifiers.

    """
    systemName = platform.system()  # get the system name
    foundCameras = []
    if systemName == 'Darwin':  # MacOS
        pass
    elif systemName == 'Linux':
        # use glob to get possible cameras connected to the system
        globResult = glob.glob(
            'video*',
            root_dir=VIDEO_DEVICE_ROOT_LINUX,
            recursive=False)
        foundCameras.extend(globResult)
        # ensure the glob gives values in the same order
        foundCameras.sort()
    elif systemName == 'Windows':
        videoDevs, _, names = list_dshow_devices()
        for devKey in videoDevs.keys():
            nameHR = names.get(devKey, None)
            ident = devKey if nameHR is None else nameHR
            foundCameras.append(ident)
    else:
        raise OSError(
            "Cannot get cameras, unsupported platform '{}'.".format(
                systemName))

    return foundCameras


if __name__ == "__main__":
    pass
