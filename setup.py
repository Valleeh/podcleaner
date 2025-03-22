from setuptools import setup, find_packages

setup(
    name="podcleaner",
    version="0.1.0",
    description="A microservices-based application for automatically cleaning podcasts by removing advertisements",
    author="PodCleaner Team",
    author_email="info@podcleaner.com",
    packages=find_packages(),
    python_requires=">=3.10",
    install_requires=[
        # Dependencies will be installed from requirements.txt in CI
    ],
) 