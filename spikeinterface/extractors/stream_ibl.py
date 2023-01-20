from io import StringIO
from typing import List, Optional, Union
from contextlib import redirect_stderr
from pathlib import Path

import numpy as np
import probeinterface as pi

from spikeinterface.core import BaseRecording, BaseRecordingSegment
from spikeinterface.core.core_tools import define_function_from_class

try:
    import brainbox
    from one.api import ONE

    HAVE_BRAINBOX_ONE = True
except ModuleNotFoundError:
    HAVE_BRAINBOX_ONE = False


class StreamingIblExtractor(BaseRecording):
    """
    Stream IBL data as an extractor object.

    Parameters
    ----------
    session : str
        The session ID to extract recordings for.
        In ONE, this is sometimes referred to as the 'eid'.
        When doing a session lookup such as

            sessions = one.alyx.rest('sessions', 'list', tag='2022_Q2_IBL_et_al_RepeatedSite')

        each returned value in `sessions` refers to it as the 'id'.
    stream_name : str
        The name of the stream to load for the session.
        These can be retrieved from calling `StreamingIblExtractor.get_stream_names(session="<your session ID>")`.
    load_sync_channels : bool, default: false
        Load or not the last channel (sync).
        If not then the probe is loaded.
    cache_folder : str, optional
        The location to temporarily store chunks of data during streaming.
        The default uses the folder designated by ONE.alyx._par.CACHE_DIR / "cache".
    remove_cached : bool, default: true
        Whether or not to remove data from the cache after it is read.
        If you expect to reuse fetched data many times, and have the disk space available, it is recommended to set this to False.

    Returns
    -------
    recording : StreamingIblExtractor
        The recording extractor which allows access to the traces.
    """

    extractor_name = "StreamingIbl"
    has_default_locations = True
    installed = HAVE_BRAINBOX_ONE
    mode = "folder"
    installation_mesg = (
        "To use the StreamingIblExtractor, install ONE-api and ibllib: \n\n pip install ONE-api\npip install ibllib\n"
    )
    name = "stream_ibl"

    @classmethod
    def get_stream_names(cls, session: str) -> List[str]:
        """
        Convenient retrieval of available stream names.

        Parameters
        ----------
        session : str
          DESCRIPTION.

        Returns
        -------
        stream_names : list of str
            List of stream names as expected by the `stream_name` argument for the class initialization.
        """
        assert HAVE_BRAINBOX_ONE, cls.installation_mesg
        one = ONE(base_url="https://openalyx.internationalbrainlab.org", password="international", silent=True)

        dataset_contents = one.list_datasets(eid=session, collection="raw_ephys_data/*")
        raw_contents = [dataset_content for dataset_content in dataset_contents if not dataset_content.endswith(".npy")]
        probe_labels = set([raw_content.split("/")[1] for raw_content in raw_contents])

        stream_names = list()
        for probe_label in probe_labels:
            raw_suffixes_by_probe = set(
                [Path(raw_content).suffixes[-2] for raw_content in raw_contents if "probe00" in raw_content]
            )
            if ".ap" in raw_suffixes_by_probe:
                stream_names.append(probe_label + ".ap")
            if ".lf" in raw_suffixes_by_probe:
                stream_names.append(probe_label + ".lf")

        return stream_names

    def __init__(
        self,
        session: str,
        stream_name: str,
        load_sync_channel: bool = False,
        cache_folder: Optional[Union[Path, str]] = None,
        remove_cached: bool = True,
    ):
        assert HAVE_BRAINBOX_ONE, self.installation_mesg
        from brainbox.io.spikeglx import Streamer
        from one.api import ONE
        from neo.rawio.spikeglxrawio import read_meta_file, extract_stream_info

        one = ONE(base_url="https://openalyx.internationalbrainlab.org", password="international", silent=True)

        session_names = self.get_stream_names(session=session)
        assert stream_name in session_names, (
            f"The `stream_name` '{stream_name}' was not found in the available listing for session '{session}'! "
            f"Please choose one of {session_names}."
        )
        probe_label, stream_type = stream_name.split(".")

        insertions = one.alyx.rest("insertions", "list", session=session)
        pid = next(insertion["id"] for insertion in insertions if insertion["name"] == probe_label)

        cache_folder = Path(cache_folder) if cache_folder is not None else cache_folder
        self._file_streamer = Streamer(
            pid=pid, one=one, typ=stream_type, cache_folder=cache_folder, remove_cached=remove_cached
        )

        # get basic metadata
        meta_file = self._file_streamer.file_meta_data  # streamer downloads uncompressed metadata files on init
        meta = read_meta_file(meta_file)
        info = extract_stream_info(meta_file, meta)
        channel_ids = info["channel_names"]
        gains = info["channel_gains"]
        offsets = info["channel_offsets"]
        if not load_sync_channel:
            channel_ids = channel_ids[:-1]
            gains = gains[:-1]
            offsets = offsets[:-1]

        # initialize main extractor
        sampling_frequency = self._file_streamer.fs
        dtype = self._file_streamer.dtype
        BaseRecording.__init__(self, channel_ids=channel_ids, sampling_frequency=sampling_frequency, dtype=dtype)
        self.extra_requirements.append("ONE-api")
        self.extra_requirements.append("ibllib")
        # traces are already scaled
        self.set_channel_gains(1.)
        self.set_channel_offsets(0.)
        self.set_property("channel_gain", gains)
        self.set_property("channel_offsets", offsets)

        # set probe
        if not load_sync_channel:
            probe = pi.read_spikeglx(meta_file)

            if probe.shank_ids is not None:
                self.set_probe(probe, in_place=True, group_mode="by_shank")
            else:
                self.set_probe(probe, in_place=True)

        # set channel properties
        # sometimes there are missing metadata files on the IBL side
        # when this happens a statement is printed to stderr saying these are using default metadata configurations
        with redirect_stderr(StringIO()):
            electrodes_geometry = self._file_streamer.geometry

        if not load_sync_channel:
            shank = electrodes_geometry["shank"]
            shank_row = electrodes_geometry["row"]
            shank_col = electrodes_geometry["col"]
            inter_sample_shift = electrodes_geometry["sample_shift"]
            adc = electrodes_geometry["adc"]
            index_on_probe = electrodes_geometry["ind"]
            good_channel = electrodes_geometry["flag"]
        else:
            shank = np.concatenate((electrodes_geometry["shank"], [np.nan]))
            shank_row = np.concatenate((electrodes_geometry["shank"], [np.nan]))
            shank_col = np.concatenate((electrodes_geometry["shank"], [np.nan]))
            inter_sample_shift = np.concatenate((electrodes_geometry["sample_shift"], [np.nan]))
            adc = np.concatenate((electrodes_geometry["adc"], [np.nan]))
            index_on_probe = np.concatenate((electrodes_geometry["ind"], [np.nan]))
            good_channel = np.concatenate((electrodes_geometry["shank"], [1.]))

        self.set_property("shank", shank)
        self.set_property("shank_row", shank_row)
        self.set_property("shank_col", shank_col)
        self.set_property("inter_sample_shift", inter_sample_shift)
        self.set_property("adc", adc)
        self.set_property("index_on_probe", index_on_probe)
        if not all(good_channel):
            self.set_property("good_channel", good_channel)

        # init recording segment
        recording_segment = StreamingIblRecordingSegment(
            file_streamer=self._file_streamer,
            load_sync_channel=load_sync_channel
        )
        self.add_recording_segment(recording_segment)

        self._kwargs = {
            "session": session,
            "stream_name": stream_name,
            "load_sync_channel": load_sync_channel,
            "cache_folder": cache_folder,
            "remove_cached": remove_cached,
        }


class StreamingIblRecordingSegment(BaseRecordingSegment):
    def __init__(self, file_streamer, load_sync_channel: bool = False):
        BaseRecordingSegment.__init__(self, sampling_frequency=file_streamer.fs)
        self._file_streamer = file_streamer
        self._load_sync_channel = load_sync_channel

    def get_num_samples(self):
        return self._file_streamer.ns

    def get_traces(self, start_frame, end_frame, channel_indices):
        if start_frame is None:
            start_frame = 0
        if end_frame is None:
            end_frame = self.get_num_samples()
        if channel_indices is None:
            channel_indices = slice(None)

        traces = self._file_streamer[start_frame:end_frame] * 1e6
        if not self._load_sync_channel:
            traces = traces[:, :-1]

        return traces[:, channel_indices]


read_streaming_ibl = define_function_from_class(source_class=StreamingIblExtractor, name="read_streaming_ibl")
