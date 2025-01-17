#!/usr/bin/env python
# -*- coding: utf-8 -*-

import argparse
import os
import re
import shutil
import sys
import wave

import numpy as np
from deepspeech import Model
from tqdm import tqdm

from audioProcessing import extract_audio, convert_samplerate
from segmentAudio import silenceRemoval
from writeToFile import write_to_file

# Line count for SRT file
line_count = 1


def sort_alphanumeric(data):
    """Sort function to sort os.listdir() alphanumerically
    Helps to process audio files sequentially after splitting

    Args:
        data : file name
    """

    convert = lambda text: int(text) if text.isdigit() else text.lower()
    alphanum_key = lambda key: [convert(c) for c in re.split('([0-9]+)', key)]

    return sorted(data, key=alphanum_key)


def ds_process_audio(ds, audio_file, output_file_handle_dict, split_duration):
    """Run DeepSpeech inference on each audio file generated after silenceRemoval
    and write to file pointed by file_handle

    Args:
        ds : DeepSpeech Model
        audio_file : audio file
        output_file_handle_dict : Mapping of subtitle format (eg, 'srt') to open file_handle
        split_duration: for long audio segments, split the subtitle based on this number of seconds
    """

    global line_count
    fin = wave.open(audio_file, 'rb')
    fs_orig = fin.getframerate()
    desired_sample_rate = ds.sampleRate()

    # Check if sampling rate is required rate (16000)
    # won't be carried out as FFmpeg already converts to 16kHz
    if fs_orig != desired_sample_rate:
        print("Warning: original sample rate ({}) is different than {}hz. Resampling might \
            produce erratic speech recognition".format(fs_orig, desired_sample_rate), file=sys.stderr)
        audio = convert_samplerate(audio_file, desired_sample_rate)
    else:
        audio = np.frombuffer(fin.readframes(fin.getnframes()), np.int16)

    fin.close()

    # Perform inference on audio segment
    metadata = ds.sttWithMetadata(audio)

    # File name contains start and end times in seconds. Extract that
    limits = audio_file.split(os.sep)[-1][:-4].split("_")[-1].split("-")

    # Run-on sentences are inferred as a single block, so write the sentence out as multiple separate lines
    # based on a user-provided split duration.
    current_token_index = 0
    split_start_index = 0
    previous_end_time = 0
    # timestamps of word boundaries
    cues = [float(limits[0])]
    num_tokens = len(metadata.transcripts[0].tokens)
    # Walk over each character in the current audio segment's inferred text
    while current_token_index < num_tokens:
        token = metadata.transcripts[0].tokens[current_token_index]
        # If at a word boundary, get the timestamp for VTT cue data
        if token.text == " ":
            cues += [float(limits[0]) + token.start_time]
        # time duration is exceeded and at the next word boundary
        needs_split = ((token.start_time - previous_end_time) > split_duration) and token.text == " "
        is_final_character = current_token_index + 1 == num_tokens
        # Write out the line
        if needs_split or is_final_character:
            # Determine the timestamps
            split_limits = [float(limits[0]) + previous_end_time, float(limits[0]) + token.start_time]
            # Convert character list to string. Upper bound has plus 1 as python list slices are [inclusive, exclusive]
            split_inferred_text = ''.join(
                [x.text for x in metadata.transcripts[0].tokens[split_start_index:current_token_index + 1]])
            write_to_file(output_file_handle_dict, split_inferred_text, line_count, split_limits, cues)
            # Reset and update indexes for the next subtitle split
            previous_end_time = token.start_time
            split_start_index = current_token_index + 1
            cues = [float(limits[0])]
            line_count += 1
        current_token_index += 1

    # For the text transcript output, after each audio segment write newlines for readability.
    if 'txt' in output_file_handle_dict.keys():
        output_file_handle_dict['txt'].write("\n\n")


def main():
    global line_count
    print("AutoSub\n")

    for x in os.listdir():
        if x.endswith(".pbmm"):
            print("Model: ", os.path.join(os.getcwd(), x))
            ds_model = os.path.join(os.getcwd(), x)
        if x.endswith(".scorer"):
            print("Scorer: ", os.path.join(os.getcwd(), x))
            ds_scorer = os.path.join(os.getcwd(), x)

    # Load DeepSpeech model
    try:
        ds = Model(ds_model)
    except:
        print("Invalid model file. Exiting\n")
        sys.exit(1)

    try:
        ds.enableExternalScorer(ds_scorer)
    except:
        print("Invalid scorer file. Running inference using only model file\n")

    supported_output_formats = ['srt', 'vtt', 'txt']
    parser = argparse.ArgumentParser(description="AutoSub")
    parser.add_argument('--file', required=True,
                        help='Input video file')
    parser.add_argument('--format', choices=supported_output_formats, nargs='+',
                        help='Create only certain output formats rather than all formats',
                        default=supported_output_formats)
    parser.add_argument('--split-duration', type=float,
                        help='Split run-on sentences exceededing this duration (in seconds) into multiple subtitles',
                        default=5)
    args = parser.parse_args()

    if os.path.isfile(args.file):
        input_file = args.file
        print("\nInput file:", input_file)
    else:
        print(args.file, ": No such file exists")
        sys.exit(1)

    base_directory = os.getcwd()
    output_directory = os.path.join(base_directory, "output")
    audio_directory = os.path.join(base_directory, "audio")
    video_prefix = os.path.splitext(os.path.basename(input_file))[0]
    audio_file_name = os.path.join(audio_directory, video_prefix + ".wav")

    output_file_handle_dict = {}
    for format in args.format:
        output_filename = os.path.join(output_directory, video_prefix + "." + format)
        print("Creating file: " + output_filename)
        output_file_handle_dict[format] = open(output_filename, "w")
        # For VTT format, write header
        if format == "vtt":
            output_file_handle_dict[format].write("WEBVTT\n")
            output_file_handle_dict[format].write("Kind: captions\n\n")

    # Clean audio/ directory
    for filename in os.listdir(audio_directory):
        if filename.lower().endswith(".wav") and filename.startswith(video_prefix):
            file_path = os.path.join(audio_directory, filename)
            try:
                if os.path.isfile(file_path) or os.path.islink(file_path):
                    os.unlink(file_path)
                elif os.path.isdir(file_path):
                    shutil.rmtree(file_path)
            except Exception as e:
                print('Failed to delete %s. Reason: %s' % (file_path, e))

    # Extract audio from input video file
    extract_audio(input_file, audio_file_name)

    print("Splitting on silent parts in audio file")
    silenceRemoval(audio_file_name)

    print("\nRunning inference:")

    for filename in tqdm(sort_alphanumeric(os.listdir(audio_directory))):
        # Only run inference on relevant files, and don't run inference on the original audio file
        if filename.startswith(video_prefix) and (filename != os.path.basename(audio_file_name)):
            audio_segment_path = os.path.join(audio_directory, filename)
            ds_process_audio(ds, audio_segment_path, output_file_handle_dict, split_duration=args.split_duration)

    print("\n")
    for format in output_file_handle_dict:
        file_handle = output_file_handle_dict[format]
        print(format.upper() + " file saved to", file_handle.name)
        file_handle.close()


if __name__ == "__main__":
    main()
