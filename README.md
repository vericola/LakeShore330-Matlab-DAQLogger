# LakeShore330-Matlab-DAQLogger

A robust, real-time Data Acquisition (DAQ) logger and live plotter for the **Lake Shore Model 330 Autotuning Temperature Controller** written in MATLAB.

## Key Features
- **Persistent Buffer Architecture**: Protects your scientific data against sudden runtime exceptions or user interruptions using a pseudo-isolated state machine (`BufferManager`).
- **Dynamic Window Tracking**: Tracks temperature differentials based on a parameterized moving time window.
- **Automatic Resource Recovery**: Uses MATLAB's `onCleanup` handler to automatically dump cached data to `.csv`/`.xlsx` and export dual-plots even if the experiment is violently aborted (`Ctrl+C`).
- **Simulation Mode Included**: Run full logging and UI loops with simulated cryogenic profiles without physical hardware connected (`USE_SIMULATION = true`).

## System Requirements
- MATLAB (with Instrument Control Toolbox)
- National Instruments (NI) 488.2 Drivers (for physical GPIB communication)

## Hardware Configurations (Lake Shore 330 Rear Panel)
Before executing the script on real hardware, make sure the DIP switches on the rear panel of your Lake Shore Model 330 are configured to match your connection parameters (e.g., IEEE-488 address, baud rate, and parity settings) as verified in the instrument user manual.

## Usage
1. Open `LakeShore330v4.m` in MATLAB.
2. For testing without an instrument, set `USE_SIMULATION = true;`.
3. For laboratory experiments, set `USE_SIMULATION = false;`.
4. Run the function:
   ```matlab
   LakeShore330v4()
