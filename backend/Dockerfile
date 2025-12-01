# Use the official Python image as the base image
FROM python:3.12-slim

# Set the working directory in the container
WORKDIR /app

# Copy the requirements file
COPY requirements.txt .

# Install the necessary packages
RUN pip install --no-cache-dir --root-user-action ignore -r requirements.txt

# Copy the application code to the container
COPY ./app /app

# Set the PYTHONPATH environment variable
ENV PYTHONPATH=/app

# Expose the port the app runs on
EXPOSE 8000

# Expose the mqtt broker
EXPOSE 1883

# Command to run the application
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]
